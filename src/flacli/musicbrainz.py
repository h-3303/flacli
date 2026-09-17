# SPDX-License-Identifier: GPL-3.0-or-later
"""MusicBrainz canonicalisation: ISRC lookup first, then artist + title (+ duration) search.

Rules from https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting: at most one request per second
per client and a meaningful User-Agent. Responses are cached in SQLite (mb_cache).
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .db import Database
from .textnorm import normalize, token_overlap

BASE_URL = "https://musicbrainz.org/ws/2"
CACHE_TTL_S = 30 * 24 * 3600
MIN_INTERVAL_S = 1.0


class MusicBrainzError(Exception):
    pass


def _lucene_escape(text: str) -> str:
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in text)


def default_fetch(url: str, user_agent: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}

        raise MusicBrainzError(f"MusicBrainz returned HTTP {error.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise MusicBrainzError(f"MusicBrainz unreachable: {error}") from None


class MusicBrainzClient:

    def __init__(self, db: Database, user_agent: str, fetch=None, min_interval_s=MIN_INTERVAL_S, sleep=time.sleep):
        self.db = db
        self.user_agent = user_agent
        self._fetch = fetch or default_fetch
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._last_request = 0.0
        self.requests_made = 0
        self.cache_hits = 0

    # HTTP with cache + rate limit #

    def get(self, endpoint: str, **params) -> dict:
        params.setdefault("fmt", "json")
        url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
        cached = self.db.cache_get(url, max_age_s=CACHE_TTL_S)

        if cached is not None:
            self.cache_hits += 1
            return cached

        wait = self._min_interval - (time.monotonic() - self._last_request)

        if wait > 0:
            self._sleep(wait)

        self._last_request = time.monotonic()
        self.requests_made += 1
        data = self._fetch(url, self.user_agent)
        self.db.cache_put(url, data)
        return data

    # Resolution #

    def resolve(self, title: str, artist: str, album: str = "", duration_ms: int | None = None,
                isrc: str | None = None) -> dict | None:
        """Return {recording_id, release_id, release_track_count, duration_ms, title, artist, method} or None."""
        if isrc:
            data = self.get(f"isrc/{urllib.parse.quote(isrc)}", inc="releases+artist-credits+media")
            recordings = data.get("recordings") or []

            if recordings:
                best = self._pick_recording(recordings, title, artist, duration_ms, prefer_query_score=False)

                if best is not None:
                    return self._describe(best, album, "isrc")

        if not title:
            return None

        query = f'recording:"{_lucene_escape(title)}"'

        if artist:
            query += f' AND artist:"{_lucene_escape(artist)}"'

        data = self.get("recording", query=query, limit=10)
        best = self._pick_recording(data.get("recordings") or [], title, artist, duration_ms, prefer_query_score=True)
        return self._describe(best, album, "search") if best is not None else None

    def _pick_recording(self, recordings, title, artist, duration_ms, prefer_query_score):
        best, best_score = None, 0.0

        for recording in recordings:
            credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in recording.get("artist-credit") or [])
            score = 0.5 * token_overlap(title, recording.get("title"))

            if artist:
                score += 0.3 * token_overlap(artist, credit)
            else:
                score += 0.3

            length = recording.get("length")

            if duration_ms and length:
                delta = abs(length - duration_ms) / 1000
                score += 0.2 if delta <= 3 else 0.1 if delta <= 10 else 0.0
            else:
                score += 0.1

            if prefer_query_score:
                score += 0.05 * (recording.get("score") or 0) / 100

            if score > best_score:
                best, best_score = recording, score

        return best if best_score >= 0.55 else None

    @staticmethod
    def _release_track_count(release) -> int | None:
        media = release.get("media") or []
        counts = [m.get("track-count") for m in media if m.get("track-count") is not None]
        return sum(counts) if counts else release.get("track-count")

    def _describe(self, recording, album, method) -> dict:
        releases = recording.get("releases") or []
        chosen = None
        album_norm = normalize(album)

        def rank(release):
            group = release.get("release-group") or {}
            return (
                normalize(release.get("title")) == album_norm if album_norm else False,
                (release.get("status") or "").lower() == "official",
                (group.get("primary-type") or "").lower() == "album",
                not (group.get("secondary-types") or []),
                release.get("date") or "9999",
            )

        if releases:
            chosen = sorted(releases, key=lambda r: (not rank(r)[0], not rank(r)[1], not rank(r)[2], not rank(r)[3], rank(r)[4]))[0]

        credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in recording.get("artist-credit") or [])
        return {
            "recording_id": recording["id"],
            "title": recording.get("title"),
            "artist": credit or None,
            "duration_ms": recording.get("length"),
            "release_id": chosen["id"] if chosen else None,
            "release_title": chosen.get("title") if chosen else None,
            "release_track_count": self._release_track_count(chosen) if chosen else None,
            "method": method,
        }

    # Releases (album requests) #

    def find_release(self, artist: str, album: str) -> dict | None:
        """Best official release for an artist + album title: {release_id, title, artist, date, track_count} or None."""
        if not album:
            return None

        query = f'release:"{_lucene_escape(album)}"'

        if artist:
            query += f' AND artist:"{_lucene_escape(artist)}"'

        data = self.get("release", query=query, limit=15)
        best, best_key = None, None

        for release in data.get("releases") or []:
            credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in release.get("artist-credit") or [])
            identity = 0.6 * token_overlap(album, release.get("title")) + (0.4 * token_overlap(artist, credit) if artist else 0.4)

            if identity < 0.55:
                continue

            group = release.get("release-group") or {}
            key = (
                round(identity, 2),
                (release.get("status") or "").lower() == "official",
                (group.get("primary-type") or "").lower() == "album",
                not (group.get("secondary-types") or []),
                -(self._release_track_count(release) or 999),   # fewer tracks = the plain edition, not the deluxe one
                release.get("date") and -int(release["date"][:4]),
                (release.get("score") or 0),
            )

            if best_key is None or key > best_key:
                best, best_key = release, key

        if best is None:
            return None

        credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in best.get("artist-credit") or [])
        return {"release_id": best["id"], "title": best.get("title"), "artist": credit or artist, "date": best.get("date"),
                "track_count": self._release_track_count(best)}

    def release_tracks(self, release_id: str) -> list[dict]:
        """Tracklist of a release in order: [{position, title, artist, recording_id, duration_ms}]."""
        data = self.get(f"release/{urllib.parse.quote(release_id)}", inc="recordings+artist-credits+media")
        album_credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in data.get("artist-credit") or [])
        tracks = []

        for medium in sorted(data.get("media") or [], key=lambda m: m.get("position") or 0):
            for track in sorted(medium.get("tracks") or [], key=lambda t: t.get("position") or 0):
                recording = track.get("recording") or {}
                credit = " ".join(c.get("name") or c.get("artist", {}).get("name", "")
                                  for c in (track.get("artist-credit") or recording.get("artist-credit") or []))
                tracks.append({
                    "position": len(tracks) + 1,
                    "title": track.get("title") or recording.get("title") or "",
                    "artist": credit or album_credit,
                    "recording_id": recording.get("id"),
                    "duration_ms": track.get("length") or recording.get("length"),
                })

        return tracks
