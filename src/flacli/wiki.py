# SPDX-License-Identifier: GPL-3.0-or-later
"""Artist bios and album wikis: sidecar files in the music folder, pushed into the player's metadata cache.

The source of truth is a Markdown file beside the music: `<Artist>/artist.md` for an artist and
`<album folder>/wiki.md` for an album, each with a small front matter (url, attribution, when written) and the
text below it. `push` copies the text into Flaclify's / Euphonica's metadata.sqlite, where the player shows it
in its wiki panel and can back it up to MPD stickers itself.

Who writes the text: `fill` takes the Wikipedia lead paragraph verbatim (CC BY-SA, attributed) when
MusicBrainz links the artist or release group to a Wikidata item with an English article. Everything else is
for an agent to write from `sources`, which returns the MusicBrainz facts and outbound links; the agent then
calls `set_text` with its prose and an attribution naming what it drew on.

Nothing here touches Soulseek. Network: MusicBrainz (1 req/s, cached), Wikidata and Wikipedia (same courtesy).
"""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import bsonlite
from .db import Database
from .musicbrainz import BUSY_BACKOFF_S, MusicBrainzClient, MusicBrainzError

ARTIST_FILE = "artist.md"
ALBUM_FILE = "wiki.md"
LOOSE_DIR = ".wiki"                      # artist files whose artist has no folder of their own
CACHE_TTL_S = 30 * 24 * 3600
MIN_INTERVAL_S = 1.0
WIKIPEDIA_ATTRIBUTION = "Wikipedia contributors, CC BY-SA 4.0"
TARGETS = {
    "flaclify": "~/.cache/flaclify/metadata.sqlite",
    "euphonica": "~/.cache/euphonica/metadata.sqlite",
}
LINK_TYPES = ("wikidata", "wikipedia", "discogs", "bandcamp", "allmusic", "official homepage", "youtube", "streaming",
              "free streaming", "lyrics", "last.fm", "purchase for download", "purchase for mail-order")


class WikiError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _fold(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _safe_name(text: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", text).strip(" .") or "_"


# Inventory #

def inventory(db: Database, root: Path) -> tuple[list[dict], list[dict]]:
    """Albums and artists from the library index under root (run a scan first). Albums are grouped by folder and
    album tag; an album's artist is its albumartist tag, else the most common track artist."""
    root = Path(root).expanduser().resolve()
    rows = db.conn.execute(
        "SELECT path, title, artist, albumartist, album, mb_release_id, mb_artist_id, format FROM library_files "
        "WHERE path LIKE ? ORDER BY path", (str(root).rstrip("/") + "/%",)).fetchall()
    groups: dict[tuple, list] = {}

    for row in rows:
        folder = str(Path(row["path"]).parent.relative_to(root))
        groups.setdefault((folder, row["album"] or ""), []).append(row)

    albums = []

    for (folder, album), tracks in groups.items():
        if not album:
            continue

        credits = Counter(r["albumartist"] for r in tracks if r["albumartist"])
        artists = Counter(r["artist"] for r in tracks if r["artist"])
        albumartist = credits.most_common(1)[0][0] if credits else None
        artist = albumartist or (artists.most_common(1)[0][0] if artists else None)
        folder_rel = "" if folder == "." else folder
        albums.append({
            "kind": "album", "artist": artist, "albumartist": albumartist, "title": album, "folder": folder_rel,
            "mbid": next((r["mb_release_id"] for r in tracks if r["mb_release_id"]), None),
            "artist_mbid": next((r["mb_artist_id"] for r in tracks if r["mb_artist_id"]), None),
            "year": _year_of(Path(tracks[0]["path"])),
            "tracks": [r["title"] for r in tracks],
            "formats": sorted({r["format"] for r in tracks if r["format"]}),
            "sidecar": str(Path(folder_rel) / ALBUM_FILE) if folder_rel else ALBUM_FILE,
        })

    by_artist: dict[str, list] = {}

    for album in albums:
        if album["artist"]:
            by_artist.setdefault(album["artist"], []).append(album)

    artists = []

    for name, owned in sorted(by_artist.items(), key=lambda item: _fold(item[0])):
        artists.append({
            "kind": "artist", "name": name,
            "mbid": next((a["artist_mbid"] for a in owned if a["artist_mbid"]), None),
            "albums": [a["title"] for a in owned],
            "sidecar": _artist_sidecar(name, owned),
        })

    albums.sort(key=lambda a: (_fold(a["artist"]), a["year"] or "", _fold(a["title"])))
    return albums, artists


def _year_of(path: Path) -> str | None:
    try:
        from mutagen import File
        audio = File(str(path), easy=True)
        value = (audio.tags or {}).get("date") if audio else None
    except Exception:   # noqa: BLE001 - a year is a nicety
        return None

    if isinstance(value, list):
        value = value[0] if value else None

    return str(value)[:4] if value else None


def _artist_sidecar(name: str, albums: list[dict]) -> str:
    wanted = _fold(name)

    for album in albums:
        top = album["folder"].split("/", 1)[0] if album["folder"] else ""

        if top and _fold(top) == wanted:
            return f"{top}/{ARTIST_FILE}"

    return f"{LOOSE_DIR}/{_safe_name(name)}.md"


def find_entry(albums: list[dict], artists: list[dict], artist: str, album: str | None = None) -> dict:
    if album:
        wanted = (_fold(artist), _fold(album))
        match = next((a for a in albums if (_fold(a["artist"]), _fold(a["title"])) == wanted), None)
        what = f"album {artist!r} - {album!r}"
    else:
        match = next((a for a in artists if _fold(a["name"]) == _fold(artist)), None)
        what = f"artist {artist!r}"

    if match is None:
        raise LookupError(f"{what} is not in the library index; check the spelling (flacli wiki missing --all lists "
                          "everything) or run flacli scan")

    return match


# Sidecar files #

def read_sidecar(path: Path) -> dict | None:
    """{"meta": {...}, "content": str} or None when the file is missing or has no text."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    meta = {}
    body = text

    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)

        if end > 0:
            for line in text[4:end].splitlines():
                key, sep, value = line.partition(":")

                if sep:
                    meta[key.strip()] = value.strip()

            body = text[end + 5:]

    content = body.strip()
    return {"meta": meta, "content": content} if content else None


def write_sidecar(path: Path, meta: dict, content: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]

    for key, value in meta.items():
        if value not in (None, ""):
            lines.append(f"{key}: {str(value).replace(chr(10), ' ').strip()}")

    lines += ["---", "", content.strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _text_state(root: Path, entry: dict) -> dict | None:
    return read_sidecar(root / entry["sidecar"])


# Sources: MusicBrainz, Wikidata, Wikipedia #

def default_fetch(url: str, user_agent: str, sleep=time.sleep) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})

    for pause in BUSY_BACKOFF_S + (None,):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}

            if error.code in (503, 429) and pause is not None:
                sleep(pause)
                continue

            raise WikiError(f"HTTP {error.code} for {url}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise WikiError(f"unreachable: {url} ({error})") from None


class Sources:
    """Facts and links for one artist or album. MusicBrainz goes through the shared rate-limited client; Wikidata
    and Wikipedia through the same cache table at the same one request per second."""

    def __init__(self, db: Database, user_agent: str, fetch=None, sleep=time.sleep, mb: MusicBrainzClient | None = None):
        self.db = db
        self.user_agent = user_agent
        self._fetch = fetch or default_fetch
        self._sleep = sleep
        self._last = 0.0
        self.mb = mb or MusicBrainzClient(db, user_agent, fetch=fetch, sleep=sleep)

    def _json(self, url: str) -> dict:
        cached = self.db.cache_get(url, max_age_s=CACHE_TTL_S)

        if cached is not None:
            return cached

        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)

        if wait > 0:
            self._sleep(wait)

        self._last = time.monotonic()
        data = self._fetch(url, self.user_agent)
        self.db.cache_put(url, data)
        return data

    # MusicBrainz #

    def _mb(self, endpoint: str, **params) -> dict:
        try:
            return self.mb.get(endpoint, **params)
        except MusicBrainzError as error:
            raise WikiError(str(error)) from None

    @staticmethod
    def _links(relations: list) -> list[dict]:
        links = []

        for relation in relations or []:
            kind = relation.get("type")
            url = (relation.get("url") or {}).get("resource")

            if url and kind in LINK_TYPES:
                links.append({"type": kind, "url": url})

        return links

    def artist(self, name: str, mbid: str | None = None) -> dict:
        if not mbid:
            found = self._mb("artist", query=f'artist:"{_escape(name)}"', limit=5).get("artists") or []
            best = next((a for a in found if int(a.get("score", 0)) >= 90 and _fold(a.get("name")) == _fold(name)), None)
            mbid = best["id"] if best else None

        result = {"kind": "artist", "name": name, "mbid": mbid, "musicbrainz": None, "links": [], "wikipedia": None}

        if not mbid:
            result["note"] = "not found on MusicBrainz by name; tag the files with MUSICBRAINZ_ARTISTID to pin it"
            return result

        data = self._mb(f"artist/{mbid}", inc="url-rels+annotation+tags+aliases")

        if not data:
            result["note"] = f"MusicBrainz has no artist {mbid}"
            return result

        span = data.get("life-span") or {}
        result["musicbrainz"] = {
            "name": data.get("name"), "type": data.get("type"), "disambiguation": data.get("disambiguation") or None,
            "country": data.get("country"), "area": (data.get("area") or {}).get("name"),
            "begin_area": (data.get("begin-area") or {}).get("name"),
            "begin": span.get("begin"), "end": span.get("end"), "ended": span.get("ended"),
            "tags": [t["name"] for t in sorted(data.get("tags") or [], key=lambda t: -int(t.get("count", 0)))[:8]],
            "annotation": data.get("annotation") or None,
            "url": f"https://musicbrainz.org/artist/{mbid}",
        }
        result["links"] = self._links(data.get("relations"))
        result["wikipedia"] = self.wikipedia(result["links"])
        return result

    def album(self, artist: str, title: str, release_mbid: str | None = None) -> dict:
        result = {"kind": "album", "artist": artist, "title": title, "mbid": release_mbid, "release_group": None,
                  "musicbrainz": None, "links": [], "wikipedia": None}
        group_id = None
        release = {}

        if release_mbid:
            release = self._mb(f"release/{release_mbid}", inc="release-groups+artist-credits+labels")
            group_id = (release.get("release-group") or {}).get("id")

        if not group_id:
            query = f'releasegroup:"{_escape(title)}" AND artist:"{_escape(artist)}"'
            found = self._mb("release-group", query=query, limit=5).get("release-groups") or []
            best = next((g for g in found if int(g.get("score", 0)) >= 90 and _fold(g.get("title")) == _fold(title)), None)
            group_id = best["id"] if best else None

        if not group_id:
            result["note"] = "not found on MusicBrainz; tag the files with MUSICBRAINZ_ALBUMID to pin the release"
            return result

        group = self._mb(f"release-group/{group_id}", inc="url-rels+annotation+tags+artist-credits")

        if not group:
            result["note"] = f"MusicBrainz has no release group {group_id}"
            return result

        result["release_group"] = group_id
        credit = "".join((c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
                         for c in group.get("artist-credit") or [])
        result["musicbrainz"] = {
            "title": group.get("title"), "artist_credit": credit or None,
            "type": " + ".join([group.get("primary-type") or ""] + list(group.get("secondary-types") or [])).strip(" +") or None,
            "first_released": group.get("first-release-date") or None, "disambiguation": group.get("disambiguation") or None,
            "labels": sorted({(l.get("label") or {}).get("name") for l in release.get("label-info") or [] if (l.get("label") or {}).get("name")}),
            "release_date": release.get("date") or None, "release_country": release.get("country") or None,
            "tags": [t["name"] for t in sorted(group.get("tags") or [], key=lambda t: -int(t.get("count", 0)))[:8]],
            "annotation": group.get("annotation") or None,
            "url": f"https://musicbrainz.org/release-group/{group_id}",
        }
        result["links"] = self._links(group.get("relations"))
        result["wikipedia"] = self.wikipedia(result["links"])
        return result

    # Wikidata -> Wikipedia #

    def wikipedia(self, links: list[dict]) -> dict | None:
        title = None

        for link in links:
            if link["type"] == "wikipedia" and "en.wikipedia.org/wiki/" in link["url"]:
                title = urllib.parse.unquote(link["url"].split("/wiki/", 1)[1]).replace("_", " ")
                break

        if title is None:
            item = next((l["url"].rstrip("/").rsplit("/", 1)[1] for l in links if l["type"] == "wikidata"), None)

            if not item:
                return None

            entity = self._json(f"https://www.wikidata.org/wiki/Special:EntityData/{item}.json")
            sitelinks = ((entity.get("entities") or {}).get(item) or {}).get("sitelinks") or {}
            title = (sitelinks.get("enwiki") or {}).get("title")

            if not title:
                return None

        params = urllib.parse.urlencode({"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
                                         "format": "json", "formatversion": 2, "titles": title})
        data = self._json(f"https://en.wikipedia.org/w/api.php?{params}")
        pages = ((data.get("query") or {}).get("pages") or [])
        page = pages[0] if pages else {}
        extract = (page.get("extract") or "").strip()

        if not extract or page.get("missing"):
            return None

        final_title = page.get("title") or title
        return {"title": final_title, "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(final_title.replace(" ", "_"), safe="/()':,!*"),
                "extract": extract, "attribution": WIKIPEDIA_ATTRIBUTION}


def _escape(text: str) -> str:
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in text)


# The player's cache #

def target_paths(setting: str) -> list[tuple[str, Path]]:
    """`wiki_targets`: comma-separated names (flaclify, euphonica) or paths to a metadata.sqlite."""
    result = []

    for item in (setting or "").split(","):
        item = item.strip()

        if not item:
            continue

        result.append((item, Path(TARGETS.get(item, item)).expanduser()))

    return result


def _cache_document(row, entry: dict, text: dict) -> dict:
    document = bsonlite.decode(row["data"]) if row else None
    wiki = {"content": text["content"], "attribution": text["meta"].get("attribution") or ""}

    if text["meta"].get("url"):
        wiki["url"] = text["meta"]["url"]

    if entry["kind"] == "album":
        if document is None:
            document = {"name": entry["title"], "artist": None, "tags": [], "image": []}
            if entry.get("mbid"):
                document["mbid"] = entry["mbid"]

        document["wiki"] = wiki
    else:
        if document is None:
            document = {"name": entry["name"], "tags": [], "similar": [], "image": [], "artist_type": "Other"}
            if entry.get("mbid"):
                document["mbid"] = entry["mbid"]

        document["bio"] = wiki

    return document


def push_entry(path: Path, entry: dict, text: dict) -> str:
    """Write one entry's text into a Flaclify/Euphonica metadata.sqlite. Returns what happened."""
    if not path.is_file():
        return "no database (the player has not run yet)"

    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row

    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        if not {"albums", "artists"} <= tables:
            return "not a Flaclify/Euphonica metadata database (no albums/artists tables)"

        stamp = _now()

        if entry["kind"] == "album":
            if not entry.get("mbid") and not entry.get("albumartist"):
                return "skipped: the player keys albums by albumartist tag or MusicBrainz release id, and the files have neither"

            row = None

            if entry.get("mbid"):
                row = conn.execute("SELECT rowid, data FROM albums WHERE mbid = ?", (entry["mbid"],)).fetchone()

            if row is None and entry.get("albumartist"):
                row = conn.execute("SELECT rowid, data FROM albums WHERE title = ? AND artist = ?",
                                   (entry["title"], entry["albumartist"])).fetchone()

            blob = bsonlite.encode(_cache_document(row, entry, text))

            with conn:
                if row is not None:
                    conn.execute("UPDATE albums SET data = ?, last_modified = ? WHERE rowid = ?", (blob, stamp, row["rowid"]))
                else:
                    conn.execute("INSERT INTO albums (folder_uri, mbid, title, artist, last_modified, data) VALUES (?, ?, ?, ?, ?, ?)",
                                 (entry["folder"].rstrip("/") + "/" if entry["folder"] else "", entry.get("mbid"), entry["title"],
                                  entry.get("albumartist"), stamp, blob))
        else:
            row = None

            if entry.get("mbid"):
                row = conn.execute("SELECT rowid, data FROM artists WHERE mbid = ?", (entry["mbid"],)).fetchone()

            if row is None:
                row = conn.execute("SELECT rowid, data FROM artists WHERE name = ?", (entry["name"],)).fetchone()

            blob = bsonlite.encode(_cache_document(row, entry, text))

            with conn:
                if row is not None:
                    conn.execute("UPDATE artists SET data = ?, last_modified = ? WHERE rowid = ?", (blob, stamp, row["rowid"]))
                else:
                    conn.execute("INSERT INTO artists (name, mbid, last_modified, data) VALUES (?, ?, ?, ?)",
                                 (entry["name"], entry.get("mbid"), stamp, blob))

        return "updated" if row is not None else "inserted"
    except sqlite3.Error as error:
        return f"database error: {error}"
    finally:
        conn.close()


def push(root: Path, entries: list[dict], targets: list[tuple[str, Path]]) -> dict:
    root = Path(root).expanduser().resolve()
    pushed = []
    skipped = []

    for entry in entries:
        text = _text_state(root, entry)
        label = _label(entry)

        if text is None:
            skipped.append({"entry": label, "reason": "no sidecar text"})
            continue

        pushed.append({"entry": label, "sidecar": entry["sidecar"],
                       "targets": {name: push_entry(path, entry, text) for name, path in targets}})

    return {"pushed": pushed, "skipped": skipped, "targets": {name: str(path) for name, path in targets}}


def _label(entry: dict) -> str:
    return f'{entry["artist"]} - {entry["title"]}' if entry["kind"] == "album" else entry["name"]


# Operations #

def _brief(entry: dict, root: Path) -> dict:
    """What an agent needs to write about an entry, plus the state of its text."""
    text = _text_state(root, entry)
    brief = {k: v for k, v in entry.items() if k not in ("artist_mbid",)}
    brief["has_text"] = text is not None

    if text:
        brief["attribution"] = text["meta"].get("attribution")
        brief["written"] = text["meta"].get("written")
        brief["chars"] = len(text["content"])

    return brief


def missing(db: Database, root: Path, include_all: bool = False) -> dict:
    root = Path(root).expanduser().resolve()
    albums, artists = inventory(db, root)
    entries = [_brief(e, root) for e in artists] + [_brief(e, root) for e in albums]
    wanted = entries if include_all else [e for e in entries if not e["has_text"]]
    return {
        "music_dir": str(root), "artists": len(artists), "albums": len(albums),
        "with_text": sum(1 for e in entries if e["has_text"]), "missing": sum(1 for e in entries if not e["has_text"]),
        "entries": wanted,
        "note": "artists first, then albums by artist and year. `flacli wiki fill` takes the Wikipedia lead where one exists; "
                "the rest is yours to write: `flacli wiki sources` for the facts, `flacli wiki set` with the text.",
    }


def set_text(db: Database, root: Path, artist: str, album: str | None, content: str, attribution: str, url: str | None,
             targets: list[tuple[str, Path]], force: bool = False, albums=None, artists=None) -> dict:
    root = Path(root).expanduser().resolve()
    content = (content or "").strip()
    attribution = (attribution or "").strip()

    if not content:
        raise ValueError("content is empty")
    if not attribution:
        raise ValueError("attribution is required: name what the text was drawn from (a source and licence, or the model and "
                         "its sources), it is shown under the text")

    if albums is None or artists is None:
        albums, artists = inventory(db, root)

    entry = find_entry(albums, artists, artist, album)
    path = root / entry["sidecar"]
    existing = read_sidecar(path)

    if existing and not force:
        raise ValueError(f"{path} already has text ({existing['meta'].get('attribution') or 'no attribution'}); pass --force to replace it")

    meta = {"kind": entry["kind"]}

    if entry["kind"] == "album":
        meta.update(artist=entry["artist"], title=entry["title"])
    else:
        meta.update(name=entry["name"])

    meta.update(mbid=entry.get("mbid"), url=url, attribution=attribution, written=_now())
    write_sidecar(path, meta, content)
    result = {"entry": _label(entry), "sidecar": str(path), "chars": len(content), "replaced": existing is not None}
    result["targets"] = {name: push_entry(target, entry, {"meta": meta, "content": content}) for name, target in targets}
    return result


def lead(extract: str, target_chars: int = 500) -> str:
    """Whole paragraphs from the start of a Wikipedia lead until about target_chars: the panel wants a summary,
    not the article's whole introduction."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", extract) if p.strip()]
    kept = []
    total = 0

    for paragraph in paragraphs:
        if kept and total >= target_chars:
            break

        kept.append(paragraph)
        total += len(paragraph)

    return "\n\n".join(kept)


def sources_for(sources: Sources, entry: dict) -> dict:
    if entry["kind"] == "album":
        return sources.album(entry["artist"], entry["title"], entry.get("mbid"))

    return sources.artist(entry["name"], entry.get("mbid"))


def fill(db: Database, root: Path, sources: Sources, targets: list[tuple[str, Path]], limit: int = 10) -> dict:
    """Wikipedia text for every entry without text, `limit` entries at a time; the rest come back as briefs with
    their facts and links, ready for an agent to write."""
    root = Path(root).expanduser().resolve()
    albums, artists = inventory(db, root)
    todo = [e for e in artists + albums if _text_state(root, e) is None]
    filled, to_write, errors = [], [], []

    for entry in todo[:max(0, limit)]:
        try:
            found = sources_for(sources, entry)
        except WikiError as error:
            errors.append({"entry": _label(entry), "error": str(error)})
            continue

        page = found.get("wikipedia")

        if page and len(page["extract"]) >= 40:
            result = set_text(db, root, entry.get("artist") or entry.get("name"), entry.get("title"), lead(page["extract"]),
                              page["attribution"], page["url"], targets, albums=albums, artists=artists)
            filled.append({"entry": result["entry"], "chars": result["chars"], "url": page["url"], "targets": result["targets"]})
        else:
            brief = _brief(entry, root)
            brief.update(musicbrainz=found.get("musicbrainz"), links=found.get("links"), source_note=found.get("note"))
            to_write.append(brief)

    return {
        "filled": filled, "to_write": to_write, "errors": errors,
        "remaining": max(0, len(todo) - limit),
        "note": "`filled` has Wikipedia text now. `to_write` needs prose: write it from the facts and links given "
                "(nothing beyond them), then `flacli wiki set`. Run again for the remaining entries.",
    }
