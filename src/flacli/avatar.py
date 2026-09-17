# SPDX-License-Identifier: GPL-3.0-or-later
"""Artist pictures for the player (Flaclify / Euphonica call them avatars).

    missing(db, root)                          artists in the library with no picture beside their music
    fill(db, root, pictures, targets, limit)   find one per artist: a picture already in the artist folder, the
                                               Wikidata portrait (Wikimedia Commons, licensed), a MusicBrainz
                                               image relation, then Deezer's public artist picture; save, push
    set_image(db, root, artist, source, ...)   one picture from a file or a URL
    push(root, entries, targets)               copy the sidecar pictures into the players' caches again

The picture lives beside the music as <Artist>/artist.jpg (png and webp kept as they came; an artist without
a folder of their own gets .wiki/<name>.jpg), where Navidrome and Jellyfin look too. Where each came from is
kept in <music>/.wiki/avatars.json. The push writes what the player would have written itself: a hires and a
thumbnail WebP under <cache>/images/ and two rows in metadata.sqlite's `images` table keyed `avatar:<name>`,
sized by the player's own settings (gsettings) when they can be read, by its defaults otherwise. A row with
an empty filename is the player's memo of a failed lookup; the push replaces it, so the picture shows.
"""

import html
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

from pathlib import Path

from .db import Database
from .wiki import LOOSE_DIR, Sources, WikiError, _fold, _now, find_entry, inventory

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
FOLDER_PICTURES = ("artist.jpg", "artist.jpeg", "artist.png", "artist.webp", "folder.jpg", "folder.png")
LEDGER = "avatars.json"
MIN_EDGE = 200                     # anything smaller is an icon, not a portrait
COMMONS_WIDTH = 1200
PROVIDERS = ("local", "wikidata", "musicbrainz", "deezer")
DEFAULT_SETTINGS = {"max": 1024, "thumb": 128, "lossless": False}     # the players' schema defaults
SCHEMAS = {"flaclify": "io.github.h3303.Flaclify.library", "euphonica": "io.github.htkhiem.Euphonica.library"}
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


# Sidecars #

def picture_stem(entry: dict) -> str:
    return entry["sidecar"][:-3] if entry["sidecar"].endswith(".md") else entry["sidecar"]


def find_picture(root: Path, entry: dict) -> Path | None:
    stem = picture_stem(entry)

    for ext in IMAGE_EXTS:
        path = root / (stem + ext)

        if path.is_file():
            return path

    return None


def _ledger_path(root: Path) -> Path:
    return root / LOOSE_DIR / LEDGER


def load_ledger(root: Path) -> dict:
    path = _ledger_path(root)

    if path.is_file():
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    return {}


def _record(root: Path, name: str, entry: dict):
    ledger = load_ledger(root)
    ledger[name] = entry
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=1, ensure_ascii=False, sort_keys=True)


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """(extension, width, height) of an image, or ValueError when it is not one worth keeping."""
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"not an image ({error})") from None

    ext = FORMATS.get(image.format or "")

    if not ext:
        raise ValueError(f"unsupported image format {image.format}")

    width, height = image.size

    if min(width, height) < MIN_EDGE:
        raise ValueError(f"too small ({width}x{height}; at least {MIN_EDGE} px on the short edge)")

    return ext, width, height


def save_picture(root: Path, entry: dict, data: bytes, ext: str) -> str:
    """Write the sidecar picture (replacing one in another format) and return its path relative to root."""
    stem = picture_stem(entry)

    for other in IMAGE_EXTS:
        old = root / (stem + other)

        if old.is_file() and other != ext:
            old.unlink()

    path = root / (stem + ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return stem + ext


# Sources #

def default_fetch_bytes(url: str, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise WikiError(f"HTTP {error.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise WikiError(f"unreachable: {url} ({error})") from None


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None

    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip() or None


class PictureSources:
    """Picture candidates for an artist. JSON lookups go through the wiki Sources (cached, one request a second);
    the picture bytes themselves are fetched once and kept only as the sidecar."""

    def __init__(self, sources: Sources, fetch_bytes=None):
        self.sources = sources
        self._fetch_bytes = fetch_bytes or default_fetch_bytes

    def download(self, url: str) -> bytes:
        return self._fetch_bytes(url, self.sources.user_agent)

    def commons(self, file_name: str, source: str) -> dict | None:
        """A Wikimedia Commons file: a 1200 px rendition plus author and licence from its description page."""
        query = urllib.parse.urlencode({"action": "query", "titles": f"File:{file_name}", "prop": "imageinfo",
                                        "iiprop": "url|extmetadata", "iiurlwidth": COMMONS_WIDTH, "format": "json",
                                        "formatversion": 2})
        data = self.sources._json(f"https://commons.wikimedia.org/w/api.php?{query}")
        pages = (data.get("query") or {}).get("pages") or []
        info = (pages[0].get("imageinfo") or [{}])[0] if pages else {}
        url = info.get("thumburl") or info.get("url")

        if not url:
            return None

        meta = info.get("extmetadata") or {}
        return {
            "source": source, "url": url,
            "page": f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(file_name.replace(' ', '_'))}",
            "author": _strip_html((meta.get("Artist") or {}).get("value")),
            "license": (meta.get("LicenseShortName") or {}).get("value") or None,
        }

    def wikidata(self, links: list[dict]) -> dict | None:
        """The Wikidata item's portrait (property P18), when MusicBrainz links an item."""
        item = next((l["url"].rstrip("/").rsplit("/", 1)[1] for l in links if l["type"] == "wikidata"), None)

        if not item:
            return None

        entity = self.sources._json(f"https://www.wikidata.org/wiki/Special:EntityData/{item}.json")
        claims = ((entity.get("entities") or {}).get(item) or {}).get("claims") or {}
        portraits = claims.get("P18") or []
        file_name = ((portraits[0].get("mainsnak") or {}).get("datavalue") or {}).get("value") if portraits else None
        return self.commons(file_name, "wikidata") if isinstance(file_name, str) and file_name else None

    def musicbrainz(self, links: list[dict]) -> dict | None:
        """A MusicBrainz "image" relation pointing at a Commons file (the only kind whose licence is known)."""
        for link in links:
            if link["type"] in ("image", "picture") and link["url"].startswith("https://commons.wikimedia.org/wiki/File:"):
                file_name = urllib.parse.unquote(link["url"].split("File:", 1)[1]).replace("_", " ")
                found = self.commons(file_name, "musicbrainz")

                if found:
                    return found

        return None

    def deezer(self, name: str) -> dict | None:
        """Deezer's public artist picture (no login), only for an exact name match, never its placeholder."""
        query = urllib.parse.urlencode({"q": name, "limit": 5})
        data = self.sources._json(f"https://api.deezer.com/search/artist?{query}")

        for artist in data.get("data") or []:
            url = artist.get("picture_xl") or artist.get("picture_big")

            if _fold(artist.get("name")) == _fold(name) and url and "/artist//" not in url:
                return {"source": "deezer", "url": url, "page": artist.get("link"), "author": None, "license": None}

        return None


def _folder_picture(root: Path, entry: dict) -> tuple[dict, bytes] | None:
    """A picture a tagger already left in the artist's own folder."""
    if entry["sidecar"].startswith(LOOSE_DIR + "/"):
        return None

    folder = root / Path(entry["sidecar"]).parent

    for name in FOLDER_PICTURES:
        path = folder / name

        if path.is_file():
            return {"source": "local", "url": None, "page": str(path.relative_to(root)), "author": None, "license": None}, path.read_bytes()

    return None


def find_for(pictures: PictureSources, root: Path, entry: dict, providers) -> tuple[dict | None, bytes | None, list[str]]:
    """The first provider, in order, with a usable picture: (meta, bytes, notes). Notes say what each tried
    provider came back with."""
    notes, links = [], None

    for provider in providers:
        found, data = None, None

        try:
            if provider == "local":
                pair = _folder_picture(root, entry)

                if pair:
                    found, data = pair
            elif provider in ("wikidata", "musicbrainz"):
                if links is None:
                    info = pictures.sources.artist(entry["name"], entry.get("mbid"))
                    links = info.get("links") or []

                    if info.get("note"):
                        notes.append(info["note"])

                found = pictures.wikidata(links) if provider == "wikidata" else pictures.musicbrainz(links)
            elif provider == "deezer":
                found = pictures.deezer(entry["name"])
            else:
                raise ValueError(f"unknown picture provider {provider!r} (choose from {', '.join(PROVIDERS)})")

            if found and data is None:
                data = pictures.download(found["url"])

            if found and data:
                ext, width, height = inspect_image(data)
                found.update(ext=ext, width=width, height=height)
                return found, data, notes
        except ValueError as error:
            notes.append(f"{provider}: {error}")
            continue

        notes.append(f"{provider}: nothing")

    return None, None, notes


# The player's cache #

def player_settings(target: Path) -> dict:
    """Image sizes the player uses, from its gsettings when the schema is installed; its defaults otherwise."""
    settings = dict(DEFAULT_SETTINGS)
    schema = SCHEMAS.get(target.parent.name.lower())

    if not schema:
        return settings

    for key, name, kind in (("max-image-resolution", "max", int), ("thumbnail-image-size", "thumb", int),
                            ("store-lossless-images", "lossless", bool)):
        try:
            out = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            break

        if out.returncode != 0:
            break

        value = out.stdout.strip().split()[-1]
        settings[name] = (value == "true") if kind is bool else int(value)

    return settings


def _renditions(picture: Path, settings: dict):
    from PIL import Image

    image = Image.open(picture)
    image.load()
    image = image.convert("RGB")
    width, height = image.size
    hires = image.copy()
    largest = min(settings["max"], max(width, height))
    hires.thumbnail((largest, largest), Image.LANCZOS)          # shrinks only, like the player
    short = settings["thumb"]
    box = (math.ceil(width * short / height), short) if width > height else (short, math.ceil(height * short / width))
    thumb = image.copy()
    thumb.thumbnail(box, Image.LANCZOS)
    return hires, thumb


def push_avatar(target: Path, name: str, picture: Path, settings: dict | None = None) -> str:
    """Write one artist's picture into a Flaclify/Euphonica cache as the player itself would. Returns what happened."""
    if not target.is_file():
        return "no database (the player has not run yet)"

    conn = sqlite3.connect(str(target), timeout=5)
    conn.row_factory = sqlite3.Row

    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        if "images" not in tables:
            return "not a Flaclify/Euphonica metadata database (no images table)"

        settings = settings or player_settings(target)
        images_dir = target.parent / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        hires, thumb = _renditions(picture, settings)
        key = f"avatar:{name}"
        stamp = _now()
        replaced, memo = False, False

        with conn:
            for flag, image in ((0, hires), (1, thumb)):
                old = conn.execute("SELECT filename FROM images WHERE key = ? AND is_thumbnail = ?", (key, flag)).fetchone()

                if old is not None:
                    replaced = replaced or bool(old["filename"])
                    memo = memo or not old["filename"]
                    conn.execute("DELETE FROM images WHERE key = ? AND is_thumbnail = ?", (key, flag))

                    if old["filename"]:
                        shared = conn.execute("SELECT COUNT(*) FROM images WHERE filename = ?", (old["filename"],)).fetchone()[0]

                        if not shared:
                            try:
                                (images_dir / old["filename"]).unlink()
                            except OSError:
                                pass

                file_name = uuid.uuid4().hex + ".webp"

                if settings["lossless"]:
                    image.save(images_dir / file_name, format="WEBP", lossless=True)
                else:
                    image.save(images_dir / file_name, format="WEBP", quality=90)

                conn.execute("INSERT INTO images (key, is_thumbnail, filename, last_modified) VALUES (?, ?, ?, ?)",
                             (key, flag, file_name, stamp))

        return "replaced" if replaced else "written, the player's failed-lookup memo cleared" if memo else "written"
    finally:
        conn.close()


def push(root: Path, entries: list[dict], targets: list[tuple[str, Path]]) -> dict:
    root = Path(root).expanduser().resolve()
    pushed, skipped = [], []

    for entry in entries:
        picture = find_picture(root, entry)

        if picture is None:
            skipped.append({"artist": entry["name"], "reason": "no picture beside the music"})
            continue

        pushed.append({"artist": entry["name"], "picture": str(picture.relative_to(root)),
                       "targets": {name: push_avatar(path, entry["name"], picture) for name, path in targets}})

    return {"pushed": pushed, "skipped": skipped, "targets": {name: str(path) for name, path in targets}}


# Operations #

def _brief(root: Path, entry: dict, ledger: dict) -> dict:
    picture = find_picture(root, entry)
    known = ledger.get(entry["name"]) or {}
    return {
        "name": entry["name"], "mbid": entry.get("mbid"), "albums": entry["albums"],
        "picture": str(picture.relative_to(root)) if picture else None,
        "source": known.get("source") if picture else None,
    }


def missing(db: Database, root: Path, include_all: bool = False) -> dict:
    root = Path(root).expanduser().resolve()
    _, artists = inventory(db, root)
    ledger = load_ledger(root)
    entries = [_brief(root, a, ledger) for a in artists]
    wanted = entries if include_all else [e for e in entries if not e["picture"]]
    return {
        "music_dir": str(root), "artists": len(entries),
        "with_picture": sum(1 for e in entries if e["picture"]), "missing": sum(1 for e in entries if not e["picture"]),
        "entries": wanted,
    }


def _store(root: Path, entry: dict, found: dict, data: bytes, targets) -> dict:
    rel = save_picture(root, entry, data, found["ext"])
    record = {"file": rel, "source": found["source"], "url": found.get("url"), "page": found.get("page"),
              "author": found.get("author"), "license": found.get("license"), "width": found["width"],
              "height": found["height"], "fetched": _now()}
    _record(root, entry["name"], record)
    pushed = push(root, [entry], targets)["pushed"][0]["targets"]
    return {"artist": entry["name"], "picture": rel, "source": found["source"], "url": found.get("url"),
            "page": found.get("page"), "author": found.get("author"), "license": found.get("license"),
            "size": f'{found["width"]}x{found["height"]}', "targets": pushed}


def fill(db: Database, root: Path, pictures: PictureSources, targets: list[tuple[str, Path]], limit: int = 10,
         providers: str | None = None) -> dict:
    """A picture for every artist without one, `limit` artists per call, from the first provider that has one."""
    root = Path(root).expanduser().resolve()
    chosen = [p.strip() for p in (providers or ",".join(PROVIDERS)).split(",") if p.strip()]
    unknown = [p for p in chosen if p not in PROVIDERS]

    if unknown:
        raise ValueError(f"unknown picture provider {', '.join(unknown)} (choose from {', '.join(PROVIDERS)})")

    _, artists = inventory(db, root)
    todo = [a for a in artists if find_picture(root, a) is None]
    filled, not_found, errors = [], [], []

    for entry in todo[:max(0, limit)]:
        try:
            found, data, notes = find_for(pictures, root, entry, chosen)
        except WikiError as error:
            errors.append({"artist": entry["name"], "error": str(error)})
            continue

        if found is None:
            not_found.append({"artist": entry["name"], "mbid": entry.get("mbid"), "tried": notes})
            continue

        filled.append(_store(root, entry, found, data, targets))

    return {
        "filled": filled, "not_found": not_found, "errors": errors,
        "remaining": max(0, len(todo) - limit), "providers": chosen,
        "note": "`filled` artists have a picture beside their music and in the player now. `not_found` need one from "
                "the user (a file or a URL, then `flacli avatar set`). Run again while `remaining` > 0.",
    }


def set_image(db: Database, root: Path, artist: str, source: str, pictures: PictureSources,
              targets: list[tuple[str, Path]], attribution: str | None = None) -> dict:
    """One artist's picture from a local file or a URL, saved beside the music and pushed."""
    root = Path(root).expanduser().resolve()
    albums, artists = inventory(db, root)
    entry = find_entry(albums, artists, artist)
    local = Path(source).expanduser()

    if local.is_file():
        data = local.read_bytes()
        found = {"source": "manual", "url": None, "page": str(local), "author": attribution, "license": None}
    elif source.startswith(("http://", "https://")):
        data = pictures.download(source)
        found = {"source": "manual", "url": source, "page": None, "author": attribution, "license": None}
    else:
        raise ValueError(f"{source} is neither a file nor a URL")

    ext, width, height = inspect_image(data)
    found.update(ext=ext, width=width, height=height)
    return _store(root, entry, found, data, targets)
