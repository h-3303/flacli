# SPDX-License-Identifier: GPL-3.0-or-later
"""Soulseek matching: query building, candidate scoring, confidence, album mode and the background job.

Confidence (0-1) says how likely a candidate IS the track; it is a weighted mean of the identity
components that could be evaluated:

    title      0.45  fraction of (cleaned) title tokens found in the file name
    artist     0.25  fraction of (first) artist tokens found anywhere in the path
    album      0.10  fraction of album tokens found in the parent folder (skipped when album unknown)
    duration   0.20  1.0 within tolerance, 0.5 within twice the tolerance, else 0 (skipped when unknown)

with two caps: title < 0.5 -> confidence <= 0.30; duration off by more than twice the tolerance
-> confidence <= 0.40. The ranking score adds quality (format preference, bitrate) and availability
(free slot, queue, speed, user penalties) so that among equally likely files the better source wins:

    score = 0.75 * confidence + 0.15 * quality + 0.10 * availability - 0.15 * user_penalty

See tests/test_matcher.py for the worked example table.
"""

import asyncio
import hashlib
import time

from dataclasses import dataclass, field

from .bridge import BridgeClient, BridgeError, BridgeUnavailable, RateLimited
from .db import Database
from .textnorm import clean_artist, clean_title, query_words, token_overlap

LOSSLESS = {"flac", "wav", "ape", "wv", "aif", "aiff", "dsf", "dff", "tak", "tta", "alac"}
AUDIO_EXTS = LOSSLESS | {"mp3", "ogg", "opus", "m4a", "aac", "mp4", "wma", "mpc"}
WEIGHTS = {"title": 0.45, "artist": 0.25, "album": 0.10, "duration": 0.20}


@dataclass
class MatchPrefs:
    prefer_formats: list[str] = field(default_factory=lambda: ["flac"])
    allow_formats: list[str] = field(default_factory=lambda: ["flac", "mp3", "ogg", "opus", "m4a", "wav", "ape", "wv", "aiff"])
    min_bitrate: int | None = None
    duration_tolerance_s: float = 3.0
    lossless_tolerance_s: float = 10.0
    album_mode: str = "auto"              # auto | on | off
    album_min_missing: int = 3
    harvest_seconds: float = 10.0
    poll_interval_s: float = 2.0
    max_candidates: int = 5
    max_tracks: int | None = None
    good_enough: float = 0.85             # stop trying fallback queries at this confidence
    max_attempts: int = 3
    folder_candidates: int = 3


def download_id(username, virtual_path) -> str:
    """Same id the Nicotine+ bridge computes for a transfer."""
    return hashlib.sha1(f"{username}\0{virtual_path}".encode("utf-8", "surrogateescape")).hexdigest()[:12]


def extension(path: str) -> str:
    name = path.rpartition("\\")[2]
    return name.rpartition(".")[2].lower() if "." in name else ""


# Queries #

def build_queries(title: str, artist: str = "", album: str = "") -> list[tuple[str, bool]]:
    """[(query, strict_artist)] in the order to try. Later queries need a stricter artist check."""
    ctitle, cartist = clean_title(title), clean_artist(artist)
    queries: list[tuple[str, bool]] = []

    def add(query, strict):
        if query and all(query != q for q, _ in queries):
            queries.append((query, strict))

    if cartist:
        add(query_words(cartist, ctitle), False)
    if album:
        add(query_words(ctitle, clean_title(album)), True)
    add(query_words(ctitle), True)
    return queries


# Scoring #

def score_result(track: dict, result: dict, prefs: MatchPrefs, strict_artist=False, user_failures=None) -> dict | None:
    """Score one bridge search result for a track ({title, artist, album, duration_ms}). None = rejected."""
    path = result["path"]
    ext = extension(path)

    if ext not in AUDIO_EXTS or (prefs.allow_formats and ext not in prefs.allow_formats):
        return None

    if prefs.min_bitrate and ext not in LOSSLESS and (result.get("bitrate") or 0) < prefs.min_bitrate:
        return None

    filename = path.rpartition("\\")[2].rpartition(".")[0]
    folder = path.rpartition("\\")[0]
    breakdown = {"title": token_overlap(clean_title(track["title"]), filename)}
    artist = clean_artist(track.get("artist") or "")

    if artist:
        breakdown["artist"] = token_overlap(artist, path)

        if strict_artist and breakdown["artist"] < 0.5:
            return None

    if track.get("album"):
        breakdown["album"] = token_overlap(clean_title(track["album"]), folder)

    duration = result.get("duration")
    track_duration = track.get("duration_ms")
    duration_cap = None

    if duration and track_duration:
        tolerance = prefs.lossless_tolerance_s if ext in LOSSLESS else prefs.duration_tolerance_s
        delta = abs(duration - track_duration / 1000)
        breakdown["duration"] = 1.0 if delta <= tolerance else 0.5 if delta <= 2 * tolerance else 0.0

        if delta > 2 * tolerance:
            duration_cap = 0.40

    weight_total = sum(WEIGHTS[k] for k in breakdown)
    confidence = sum(WEIGHTS[k] * v for k, v in breakdown.items()) / weight_total

    if breakdown["title"] < 0.5:
        confidence = min(confidence, 0.30)
    if duration_cap is not None:
        confidence = min(confidence, duration_cap)

    if ext in LOSSLESS:
        quality = 1.0
    else:
        quality = min((result.get("bitrate") or 128) / 320, 1.0) * 0.8

    if prefs.prefer_formats:
        quality *= 1.0 if ext in prefs.prefer_formats else 0.6

    availability = (0.5 if result.get("free_slot") else 0.15)
    availability += 0.3 / (1 + (result.get("queue") or 0) / 10)
    availability += 0.2 * min((result.get("speed") or 0) / 1_000_000, 1.0)
    penalty = min((user_failures or {}).get(result["user"], 0), 2) / 2
    score = 0.75 * confidence + 0.15 * quality + 0.10 * availability - 0.15 * penalty

    return {
        "kind": "file",
        "user": result["user"],
        "path": path,
        "name": path.rpartition("\\")[2],
        "size": result.get("size"),
        "ext": ext,
        "bitrate": result.get("bitrate"),
        "duration": duration,
        "sample_rate": result.get("sample_rate"),
        "bit_depth": result.get("bit_depth"),
        "vbr": result.get("vbr"),
        "free_slot": bool(result.get("free_slot")),
        "queue": result.get("queue") or 0,
        "speed": result.get("speed") or 0,
        "confidence": round(confidence, 3),
        "score": round(score, 3),
        "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
    }


def best_candidates(track: dict, results: list[dict], prefs: MatchPrefs, strict_artist=False, user_failures=None,
                    limit=None) -> list[dict]:
    scored = [c for c in (score_result(track, r, prefs, strict_artist, user_failures) for r in results) if c]
    scored.sort(key=lambda c: -c["score"])
    seen = set()
    unique = []

    for candidate in scored:
        key = (candidate["user"], candidate["path"])

        if key not in seen:
            seen.add(key)
            unique.append(candidate)

    return unique[: limit or prefs.max_candidates]


def score_folder(tracks: list[dict], listing: list[dict], expected_count: int | None, user: str, folder: str,
                 prefs: MatchPrefs, availability: dict | None = None) -> dict | None:
    """Score a folder listing (bridge folder_contents 'ready' files of one folder) against missing tracks."""
    audio = [f for f in listing if extension(f["path"]) in AUDIO_EXTS]

    if not audio:
        return None

    assignments = {}
    per_track = []

    for track in tracks:
        best, best_name = 0.0, None

        for entry in audio:
            overlap = token_overlap(clean_title(track["title"]), entry["name"].rpartition(".")[0])

            if overlap > best:
                best, best_name = overlap, entry

        per_track.append(best)

        if best_name is not None and best >= 0.5:
            assignments[track["id"]] = best_name["path"]

    coverage = sum(per_track) / len(per_track)
    count_component = 1.0 if expected_count is None else (1.0 if len(audio) == expected_count
                                                          else 0.6 if abs(len(audio) - expected_count) <= 1 else 0.2)
    confidence = 0.7 * coverage + 0.3 * count_component
    exts = {extension(f["path"]) for f in audio}
    quality = 1.0 if exts <= LOSSLESS else 0.5

    if prefs.prefer_formats and not (exts & set(prefs.prefer_formats)):
        quality *= 0.6

    avail = availability or {}
    availability_score = (0.5 if avail.get("free_slot") else 0.15) + 0.3 / (1 + (avail.get("queue") or 0) / 10)
    score = 0.75 * confidence + 0.15 * quality + 0.10 * availability_score

    return {
        "kind": "folder",
        "user": user,
        "folder": folder,
        "track_count": len(audio),
        "expected_track_count": expected_count,
        "formats": sorted(exts),
        "size": sum(f.get("size") or 0 for f in audio),
        "free_slot": bool(avail.get("free_slot")),
        "queue": avail.get("queue") or 0,
        "confidence": round(confidence, 3),
        "score": round(score, 3),
        "breakdown": {"coverage": round(coverage, 2), "track_count": count_component},
        "assignments": assignments,
    }


# Background job #

class MatchJob:

    def __init__(self, db: Database, bridge: BridgeClient, prefs: MatchPrefs, sleep=asyncio.sleep):
        self.db = db
        self.bridge = bridge
        self.prefs = prefs
        self.sleep = sleep
        self.job_id = None
        self.progress = {}

    def _save_progress(self, status=None, **updates):
        self.progress.update(updates)
        self.db.update_job(self.job_id, status=status, progress=self.progress)

    async def run(self, job_id: int, playlist_id: int, track_ids=None):
        """Match the playlist's pending tracks (or just track_ids); the job row is updated as it goes."""
        self.job_id = job_id
        rows = self.db.tracks(playlist_id, statuses=["pending", "searching", "not_found"])
        rows = [r for r in rows if r["status"] != "not_found" or r["attempts"] < self.prefs.max_attempts]

        if track_ids is not None:
            wanted = set(track_ids)
            rows = [r for r in rows if r["id"] in wanted]

        if self.prefs.max_tracks:
            rows = rows[: self.prefs.max_tracks]

        self.progress = {"total": len(rows), "done": 0, "searches": 0, "waiting_rate_limit_until": None,
                         "album_groups": 0, "current": None}
        self._save_progress("running")

        try:
            remaining = await self._album_pass(rows) if self.prefs.album_mode != "off" else rows

            for row in remaining:
                self._save_progress(current=f"{row['artist']} - {row['title']}")
                await self._match_track(row)
                self._save_progress(done=self.progress["done"] + 1)

            self._save_progress("finished", current=None)
        except asyncio.CancelledError:
            self._save_progress("cancelled", current=None)
            raise
        except BridgeUnavailable as error:
            self.db.update_job(job_id, status="failed", error=str(error))
        except Exception as error:  # noqa: BLE001 - recorded, never crashes the server
            self.db.update_job(job_id, status="failed", error=f"{type(error).__name__}: {error}")

    async def _search(self, query: str) -> int:
        while True:
            try:
                started = await self.bridge.call("search", query=query)
            except RateLimited as error:
                self._save_progress("waiting", waiting_rate_limit_until=time.time() + error.retry_after)
                await self.sleep(error.retry_after)
                continue

            self._save_progress("running", waiting_rate_limit_until=None, searches=self.progress["searches"] + 1)
            return started["search_id"]

    async def _harvest(self, search_id: int) -> list[dict]:
        deadline = time.monotonic() + self.prefs.harvest_seconds
        results: list[dict] = []

        while True:
            await self.sleep(min(self.prefs.poll_interval_s, max(0.0, deadline - time.monotonic())))
            data = await self.bridge.call("search_results", search_id=search_id, limit=5000)
            results = data["results"]

            if time.monotonic() >= deadline:
                break

        try:
            await self.bridge.call("stop_search", search_id=search_id)
        except BridgeError:
            pass

        return results

    async def _match_track(self, row):
        track = {"id": row["id"], "title": row["title"], "artist": row["artist"], "album": row["album"],
                 "duration_ms": row["duration_ms"]}
        self.db.set_match(row["id"], "searching", bump_attempts=True)
        failures = self.db.user_failures(self.job_id)
        candidates: list[dict] = []
        last_search = None

        for query, strict in build_queries(row["title"], row["artist"], row["album"]):
            search_id = await self._search(query)
            last_search = search_id
            results = await self._harvest(search_id)

            for candidate in best_candidates(track, results, self.prefs, strict, failures):
                candidate["query"] = query
                candidates.append(candidate)

            candidates.sort(key=lambda c: -c["score"])
            candidates = candidates[: self.prefs.max_candidates]

            if candidates and candidates[0]["confidence"] >= self.prefs.good_enough:
                break

        if candidates:
            self.db.set_match(row["id"], "candidates", candidates=candidates, confidence=candidates[0]["confidence"],
                              bridge_search_id=last_search, last_error=None)
        else:
            self.db.set_match(row["id"], "not_found", candidates=[], confidence=None, bridge_search_id=last_search,
                              last_error="no acceptable results")

    async def _album_pass(self, rows) -> list:
        """Album mode: releases with >= album_min_missing missing tracks are searched as folders."""
        groups: dict[str, list] = {}

        for row in rows:
            if row["mb_release_id"]:
                groups.setdefault(row["mb_release_id"], []).append(row)

        remaining = list(rows)
        handled: set[int] = set()

        for release_id, group in groups.items():
            if len(group) < self.prefs.album_min_missing:
                continue

            self.progress["album_groups"] += 1
            first = group[0]
            self._save_progress(current=f"album: {first['artist']} - {first['album']}")
            expected = first["mb_release_track_count"]
            query = query_words(clean_artist(first["artist"]), clean_title(first["album"]))

            if not query:
                continue

            search_id = await self._search(query)
            results = await self._harvest(search_id)
            folders: dict[tuple, dict] = {}

            for result in results:
                if extension(result["path"]) not in AUDIO_EXTS:
                    continue

                key = (result["user"], result["path"].rpartition("\\")[0])
                entry = folders.setdefault(key, {"hits": 0, "free_slot": result["free_slot"], "queue": result["queue"]})
                entry["hits"] += 1

            ranked = sorted(folders.items(), key=lambda kv: (-kv[1]["hits"], not kv[1]["free_slot"], kv[1]["queue"]))
            tracks = [{"id": r["id"], "title": r["title"]} for r in group]
            folder_candidates = []

            for (user, folder), availability in ranked[: self.prefs.folder_candidates]:
                listing = await self._folder_listing(user, folder)

                if not listing:
                    continue

                candidate = score_folder(tracks, listing, expected, user, folder, self.prefs, availability)

                if candidate:
                    folder_candidates.append(candidate)

            folder_candidates.sort(key=lambda c: -c["score"])

            if not folder_candidates:
                continue

            for row in group:
                own = []

                for candidate in folder_candidates:
                    if row["id"] in candidate["assignments"]:
                        entry = {k: v for k, v in candidate.items() if k != "assignments"}
                        entry["expected_path"] = candidate["assignments"][row["id"]]
                        own.append(entry)

                if own:
                    self.db.set_match(row["id"], "candidates", candidates=own, confidence=own[0]["confidence"],
                                      bridge_search_id=search_id, last_error=None, bump_attempts=True)
                    handled.add(row["id"])
                    self.progress["done"] += 1

            self._save_progress()

        return [r for r in remaining if r["id"] not in handled]

    async def _folder_listing(self, user, folder) -> list[dict] | None:
        try:
            await self.bridge.call("folder_contents", username=user, folder_path=folder)
        except BridgeError as error:
            if "unknown method" in str(error):
                return None  # protocol v1 plugin
            raise

        deadline = time.monotonic() + max(self.prefs.harvest_seconds, 5)

        while True:
            data = await self.bridge.call("folder_contents_result", username=user, folder_path=folder)

            if data["status"] == "ready":
                return data["folders"].get(folder, [])
            if data["status"] != "pending" or time.monotonic() >= deadline:
                return None

            await self.sleep(min(self.prefs.poll_interval_s, 1.0))
