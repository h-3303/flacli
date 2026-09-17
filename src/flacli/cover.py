# SPDX-License-Identifier: GPL-3.0-or-later
"""Album covers: a cover file beside every album and a picture in every track.

    missing(db, root)                          albums with no cover file, or tracks without an embedded picture
    fill(db, root, pictures, targets, limit)   one per album: a picture already in the folder or in a track, the
                                               Cover Art Archive front (by release, then release group), Deezer,
                                               the iTunes Search API; save, embed, push
    set_image(db, root, artist, album, ...)    one cover from a file or a URL
    push(root, entries, targets)               write the cover files into the players' caches again

The cover lives beside the music as <album folder>/cover.jpg (png and webp kept as they came), the name MPD's
`albumart` command, Navidrome, Jellyfin and most taggers look for; where each came from is kept in
<music>/.wiki/covers.json. Tracks without a picture of their own get the same image embedded (FLAC, MP3, MP4,
Ogg); a picture already in a track is never replaced. Flaclify and Euphonica key album art in their cache by
the album's folder URI and remember a failed lookup as an empty row that stops them ever asking MPD again, so
the push writes the renditions into the cache the way the player would have, clearing that memo.
"""

import base64
import io
import json
import re
import urllib.parse

from pathlib import Path

from .avatar import PictureSources, inspect_image, push_image
from .db import Database
from .wiki import LOOSE_DIR, TARGETS, WikiError, _fold, _now, find_entry, inventory, target_paths

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp")      # what MPD's albumart looks for
LOCAL_NAMES = COVER_NAMES + ("folder.jpg", "folder.jpeg", "folder.png", "front.jpg", "front.jpeg", "front.png",
                             "album.jpg", "album.png", "albumart.jpg", "albumart.png")
LEDGER = "covers.json"
MIN_EDGE = 300                    # smaller than this is a thumbnail, not a cover
PROVIDERS = ("local", "coverart", "deezer", "itunes")
AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus")
MIMES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COVERART = "https://coverartarchive.org"
FRONT_COVER = 3


# Sidecars #

def find_cover(root: Path, entry: dict) -> Path | None:
    if not entry["folder"]:
        return None

    folder = root / entry["folder"]

    for name in COVER_NAMES:
        path = folder / name

        if path.is_file():
            return path

    return None


def save_cover(root: Path, entry: dict, data: bytes, ext: str) -> str:
    """Write <folder>/cover.<ext>, replacing a cover file in another format; returns the path relative to root."""
    folder = root / entry["folder"]

    for name in COVER_NAMES:
        old = folder / name

        if old.is_file() and not name.endswith(ext):
            old.unlink()

    path = folder / f"cover{ext}"
    path.write_bytes(data)
    return str(path.relative_to(root))


def _ledger_path(root: Path) -> Path:
    return root / LOOSE_DIR / LEDGER


def load_ledger(root: Path) -> dict:
    path = _ledger_path(root)

    if path.is_file():
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    return {}


def _record(root: Path, folder: str, entry: dict):
    ledger = load_ledger(root)
    ledger[folder] = entry
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=1, ensure_ascii=False, sort_keys=True)


def inspect_cover(data: bytes) -> tuple[str, int, int]:
    ext, width, height = inspect_image(data)

    if min(width, height) < MIN_EDGE:
        raise ValueError(f"too small ({width}x{height}; at least {MIN_EDGE} px on the short edge)")

    return ext, width, height


# Embedded pictures #

def album_tracks(root: Path, entry: dict) -> list[Path]:
    folder = root / entry["folder"] if entry["folder"] else root
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def _open(path: Path):
    from mutagen import File

    try:
        return File(str(path))
    except Exception:   # noqa: BLE001 - an unreadable file simply has no picture to offer
        return None


def embedded_picture(path: Path) -> bytes | None:
    """The front cover embedded in one track (the largest picture when there is no front cover), or None."""
    audio = _open(path)

    if audio is None:
        return None

    pictures: list[tuple[int, int, bytes]] = []      # (is front, size, bytes)
    tags = audio.tags

    if hasattr(audio, "pictures"):                                              # FLAC
        pictures += [(p.type == FRONT_COVER, len(p.data), p.data) for p in audio.pictures]
    elif tags is not None and hasattr(tags, "getall"):                          # ID3
        pictures += [(p.type == FRONT_COVER, len(p.data), p.data) for p in tags.getall("APIC")]
    elif tags is not None and "covr" in tags:                                   # MP4
        pictures += [(True, len(bytes(c)), bytes(c)) for c in tags["covr"]]
    elif tags is not None and "metadata_block_picture" in tags:                 # Ogg Vorbis / Opus
        from mutagen.flac import Picture

        for encoded in tags["metadata_block_picture"]:
            try:
                picture = Picture(base64.b64decode(encoded))
            except Exception:   # noqa: BLE001 - a malformed block is no picture
                continue

            pictures.append((picture.type == FRONT_COVER, len(picture.data), picture.data))

    if not pictures:
        return None

    return max(pictures, key=lambda p: (p[0], p[1]))[2]


def embed_picture(path: Path, data: bytes, ext: str, width: int, height: int) -> bool:
    """Embed a front cover into a track that has none. Returns False when the format takes no picture."""
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    audio = _open(path)

    if audio is None:
        return False

    if ext == ".webp" and isinstance(audio, MP4):
        data, ext = _to_jpeg(data), ".jpg"

    mime = MIMES[ext]

    if isinstance(audio, FLAC):
        picture = Picture()
        picture.type, picture.mime, picture.data = FRONT_COVER, mime, data
        picture.width, picture.height, picture.depth = width, height, 24
        audio.add_picture(picture)
        audio.save()
    elif isinstance(audio, MP3):
        tags = audio.tags if audio.tags is not None else ID3()
        tags.add(APIC(encoding=3, mime=mime, type=FRONT_COVER, desc="Cover", data=data))
        tags.save(str(path))
    elif isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()

        audio.tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if ext == ".png" else MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif isinstance(audio, (OggVorbis, OggOpus)):
        picture = Picture()
        picture.type, picture.mime, picture.data = FRONT_COVER, mime, data
        picture.width, picture.height, picture.depth = width, height, 24
        audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        audio.save()
    else:
        return False

    return True


def _to_jpeg(data: bytes) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def embed_into(root: Path, entry: dict, data: bytes, ext: str, width: int, height: int) -> dict:
    """Embed the cover into every track of the album that has no picture yet."""
    embedded, kept, unsupported = [], 0, []

    for track in album_tracks(root, entry):
        if embedded_picture(track) is not None:
            kept += 1
        elif embed_picture(track, data, ext, width, height):
            embedded.append(track.name)
        else:
            unsupported.append(track.name)

    return {"embedded": embedded, "already_had_one": kept, "unsupported": unsupported}


# Sources #

def _strip_edition(title: str) -> str:
    """'Ultraviolence (Deluxe)' -> 'Ultraviolence': the second try when the exact title finds nothing."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title).strip() or title


class CoverSources(PictureSources):
    """Cover candidates for an album, through the same paced, cached fetchers as the artist pictures."""

    def probe(self, url: str) -> bytes | None:
        """Download, or None when the server has no such picture (404)."""
        try:
            return self.download(url)
        except WikiError as error:
            if "HTTP 404" in str(error):
                return None

            raise

    def coverart(self, entry: dict) -> tuple[dict, bytes] | None:
        """The Cover Art Archive front, 1200 px: the tagged release first, else the release group MusicBrainz
        finds for artist and title (a front from any release in the group)."""
        tried = []

        if entry.get("mbid"):
            tried.append(("release", entry["mbid"]))

        try:
            info = self.sources.album(entry["artist"] or "", entry["title"], entry.get("mbid"))
        except WikiError:
            info = {}                     # a stale MUSICBRAINZ_ALBUMID must not block the other providers

        if info.get("release_group"):
            tried.append(("release-group", info["release_group"]))

        for kind, mbid in tried:
            data = self.probe(f"{COVERART}/{kind}/{mbid}/front-1200")

            if data:
                return {"source": "coverart", "url": f"{COVERART}/{kind}/{mbid}/front-1200",
                        "page": f"https://musicbrainz.org/{kind}/{mbid}", "author": None, "license": None}, data

        return None

    def deezer_album(self, artist: str, title: str) -> dict | None:
        """Deezer's public album search (no login), for an exact artist and title match; the edition suffix is
        dropped for a second try."""
        for wanted in dict.fromkeys((title, _strip_edition(title))):
            query = urllib.parse.urlencode({"q": f'artist:"{artist}" album:"{wanted}"', "limit": 10})
            data = self.sources._json(f"https://api.deezer.com/search/album?{query}")

            for album in data.get("data") or []:
                url = album.get("cover_xl") or album.get("cover_big")
                name = (album.get("artist") or {}).get("name")

                if _fold(album.get("title")) == _fold(wanted) and _fold(name) == _fold(artist) and url and "/cover//" not in url:
                    return {"source": "deezer", "url": url, "page": album.get("link"), "author": None, "license": None}

        return None

    def itunes(self, artist: str, title: str) -> dict | None:
        """The iTunes Search API (no login): the 100 px artwork URL points at a 1200 px rendition too."""
        for wanted in dict.fromkeys((title, _strip_edition(title))):
            query = urllib.parse.urlencode({"term": f"{artist} {wanted}", "entity": "album", "limit": 10})
            data = self.sources._json(f"https://itunes.apple.com/search?{query}")

            for album in data.get("results") or []:
                url = album.get("artworkUrl100")

                if _fold(album.get("collectionName")) == _fold(wanted) and _fold(album.get("artistName")) == _fold(artist) and url:
                    return {"source": "itunes", "url": url.replace("100x100bb", "1200x1200bb"),
                            "page": album.get("collectionViewUrl"), "author": None, "license": None}

        return None


def _folder_picture(root: Path, entry: dict) -> tuple[dict, bytes] | None:
    """A picture a tagger already left in the album folder, else the largest one embedded in a track."""
    if not entry["folder"]:
        return None

    folder = root / entry["folder"]

    for name in LOCAL_NAMES:
        path = folder / name

        if path.is_file():
            return {"source": "local", "url": None, "page": str(path.relative_to(root)), "author": None, "license": None}, path.read_bytes()

    best: tuple[int, Path, bytes] | None = None

    for track in album_tracks(root, entry):
        data = embedded_picture(track)

        if data and (best is None or len(data) > best[0]):
            best = (len(data), track, data)

    if best:
        return {"source": "local", "url": None, "page": f"embedded in {best[1].relative_to(root)}", "author": None, "license": None}, best[2]

    return None


def find_for(pictures: CoverSources, root: Path, entry: dict, providers) -> tuple[dict | None, bytes | None, list[str]]:
    """The first provider, in order, with a usable cover: (meta, bytes, notes)."""
    notes = []

    for provider in providers:
        found, data = None, None

        try:
            if provider == "local":
                pair = _folder_picture(root, entry)

                if pair:
                    found, data = pair
            elif provider == "coverart":
                pair = pictures.coverart(entry)

                if pair:
                    found, data = pair
            elif provider == "deezer":
                found = pictures.deezer_album(entry["artist"] or "", entry["title"])
            elif provider == "itunes":
                found = pictures.itunes(entry["artist"] or "", entry["title"])
            else:
                raise ValueError(f"unknown cover provider {provider!r} (choose from {', '.join(PROVIDERS)})")

            if found and data is None:
                data = pictures.download(found["url"])

            if found and data:
                ext, width, height = inspect_cover(data)
                found.update(ext=ext, width=width, height=height)
                return found, data, notes
        except ValueError as error:
            notes.append(f"{provider}: {error}")
            continue

        notes.append(f"{provider}: nothing")

    return None, None, notes


# The player's cache #

def cover_targets(setting: str) -> list[tuple[str, Path]]:
    """The wiki_targets plus Flaclify's own cache when it exists: Flaclify reads bios and artist pictures from the
    files beside the music, but album art only through its cache, where a failed lookup is remembered."""
    targets = target_paths(setting)
    flaclify = Path(TARGETS["flaclify"]).expanduser()

    if flaclify.is_file() and all(path != flaclify for _, path in targets):
        targets.append(("flaclify", flaclify))

    return targets


def cache_key(entry: dict) -> str:
    """The player's key for an album's art: its folder URI, relative to MPD's music directory (the music_dir),
    with the trailing slash the player keeps."""
    return entry["folder"].rstrip("/") + "/"


def push(root: Path, entries: list[dict], targets: list[tuple[str, Path]]) -> dict:
    root = Path(root).expanduser().resolve()
    pushed, skipped = [], []

    for entry in entries:
        cover = find_cover(root, entry)

        if cover is None:
            skipped.append({"artist": entry["artist"], "album": entry["title"],
                            "reason": "no folder of its own" if not entry["folder"] else "no cover file beside the music"})
            continue

        pushed.append({"artist": entry["artist"], "album": entry["title"], "cover": str(cover.relative_to(root)),
                       "targets": {name: push_image(path, cache_key(entry), cover) for name, path in targets}})

    return {"pushed": pushed, "skipped": skipped, "targets": {name: str(path) for name, path in targets}}


# Operations #

def _brief(root: Path, entry: dict, ledger: dict) -> dict:
    cover = find_cover(root, entry)
    known = ledger.get(entry["folder"]) or {}
    tracks = album_tracks(root, entry) if entry["folder"] else []
    without = [t.name for t in tracks if embedded_picture(t) is None]
    return {
        "artist": entry["artist"], "album": entry["title"], "folder": entry["folder"], "mbid": entry.get("mbid"),
        "cover": str(cover.relative_to(root)) if cover else None,
        "source": known.get("source") if cover else None,
        "tracks": len(tracks), "tracks_without_picture": len(without),
    }


def _wanted(brief: dict) -> bool:
    return brief["cover"] is None or brief["tracks_without_picture"] > 0


def missing(db: Database, root: Path, include_all: bool = False) -> dict:
    root = Path(root).expanduser().resolve()
    albums, _ = inventory(db, root)
    ledger = load_ledger(root)
    entries = [_brief(root, a, ledger) for a in albums if a["folder"]]
    loose = [a for a in albums if not a["folder"]]
    wanted = entries if include_all else [e for e in entries if _wanted(e)]
    result = {
        "music_dir": str(root), "albums": len(entries),
        "with_cover": sum(1 for e in entries if e["cover"]),
        "missing": sum(1 for e in entries if e["cover"] is None),
        "tracks_without_picture": sum(e["tracks_without_picture"] for e in entries),
        "entries": wanted,
    }

    if loose:
        result["no_folder"] = [f'{a["artist"]} - {a["title"]}' for a in loose]
        result["note"] = "`no_folder` albums sit in the music directory itself and get no cover file; file them first."

    return result


def _store(root: Path, entry: dict, found: dict, data: bytes, targets, embed: bool) -> dict:
    rel = save_cover(root, entry, data, found["ext"])
    record = {"file": rel, "source": found["source"], "url": found.get("url"), "page": found.get("page"),
              "author": found.get("author"), "license": found.get("license"), "width": found["width"],
              "height": found["height"], "fetched": _now()}
    _record(root, entry["folder"], record)
    result = {"artist": entry["artist"], "album": entry["title"], "cover": rel, "source": found["source"],
              "url": found.get("url"), "page": found.get("page"), "size": f'{found["width"]}x{found["height"]}'}

    if embed:
        result["tracks"] = embed_into(root, entry, data, found["ext"], found["width"], found["height"])

    result["targets"] = push(root, [entry], targets)["pushed"][0]["targets"]
    return result


def fill(db: Database, root: Path, pictures: CoverSources, targets: list[tuple[str, Path]], limit: int = 10,
         providers: str | None = None, embed: bool = True) -> dict:
    """A cover for every album without one (and a picture in every track), `limit` albums per call."""
    root = Path(root).expanduser().resolve()
    chosen = [p.strip() for p in (providers or ",".join(PROVIDERS)).split(",") if p.strip()]
    unknown = [p for p in chosen if p not in PROVIDERS]

    if unknown:
        raise ValueError(f"unknown cover provider {', '.join(unknown)} (choose from {', '.join(PROVIDERS)})")

    albums, _ = inventory(db, root)
    ledger = load_ledger(root)
    todo, seen = [], set()

    for album in albums:
        if not album["folder"] or album["folder"] in seen:
            continue

        seen.add(album["folder"])
        brief = _brief(root, album, ledger)

        if brief["cover"] is None or (embed and brief["tracks_without_picture"]):
            todo.append(album)

    filled, not_found, errors = [], [], []

    for entry in todo[:max(0, limit)]:
        try:
            found, data, notes = find_for(pictures, root, entry, chosen)
        except WikiError as error:
            errors.append({"artist": entry["artist"], "album": entry["title"], "error": str(error)})
            continue

        if found is None:
            not_found.append({"artist": entry["artist"], "album": entry["title"], "mbid": entry.get("mbid"), "tried": notes})
            continue

        filled.append(_store(root, entry, found, data, targets, embed))

    return {
        "filled": filled, "not_found": not_found, "errors": errors,
        "remaining": max(0, len(todo) - limit), "providers": chosen, "embed": embed,
        "note": "`filled` albums have a cover file beside the music, in every track that had none, and in the player. "
                "`not_found` need one from the user (a file or a URL, then `flacli cover set`). Run again while `remaining` > 0.",
    }


def set_image(db: Database, root: Path, artist: str, album: str, source: str, pictures: CoverSources,
              targets: list[tuple[str, Path]], attribution: str | None = None, embed: bool = True) -> dict:
    """One album's cover from a local file or a URL, saved beside the music, embedded and pushed."""
    root = Path(root).expanduser().resolve()
    albums, artists = inventory(db, root)
    entry = find_entry(albums, artists, artist, album)

    if not entry["folder"]:
        raise ValueError(f"{artist} - {album} sits in the music directory itself; file it in a folder of its own first")

    local = Path(source).expanduser()

    if local.is_file():
        data = local.read_bytes()
        found = {"source": "manual", "url": None, "page": str(local), "author": attribution, "license": None}
    elif source.startswith(("http://", "https://")):
        data = pictures.download(source)
        found = {"source": "manual", "url": source, "page": None, "author": attribution, "license": None}
    else:
        raise ValueError(f"{source} is neither a file nor a URL")

    ext, width, height = inspect_cover(data)
    found.update(ext=ext, width=width, height=height)
    return _store(root, entry, found, data, targets, embed)
