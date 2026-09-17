# SPDX-License-Identifier: GPL-3.0-or-later
"""Tidy a music library: normalise tags, drop lossy duplicates of FLACs, and file everything as
``Artist/Album/NN - Title.ext``.

    Tidy(root).analyse()   dry run: writes <root>/.tidy/report.txt + plan.json, returns a summary
    Tidy(root).apply()     backup tags, write tags, delete duplicates, move files, prune, log

Working files live in ``<root>/.tidy/``:

    approved.py         persistent human decisions (canonical spellings, overrides, deletions, ...)
    report.txt          last dry-run report
    plan.json           last dry-run plan, per file
    tag_changes.log     append-only log of everything ever written, deleted or moved
    backups/            full raw-tag snapshot taken before every apply
    reports/            download reports moved out of the library

Rules the planner applies by itself (the plan is still shown before anything is written):

    R1 trim whitespace in every text tag (lyrics left alone) · R2a strip ", Album" appended to ARTIST
    by YouTube-style taggers · R2b normalise feat. · R3 set ALBUMARTIST · R4 unify album title spelling
    within an album · R6 date format, propagated across album siblings · R6c fill DATE from DATE_FILL ·
    R7 integer track/disc numbers · R10 FLAC beats lossy · R11 multi-value ARTIST -> primary ·
    R14 move to Artist/Album/NN - Title · R15 download reports out · R16 prune empty folders.

Everything else (artist spellings, missing albums, date disagreements, playlist dumps, duplicate
edits, strays) is reported as an open question and only acted on once it is in ``approved.py``.
"""

import collections
import datetime
import json
import os
import re
import unicodedata

from pathlib import Path

import mutagen
import mutagen.mp3

from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
from mutagen.mp4 import MP4, MP4FreeForm

EXT = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
LOSSLESS = {".flac"}
IMG = {".jpg", ".jpeg", ".png"}
EXTRA = {".pdf", ".nfo"}
# The sidecars flacli itself writes beside the music (wiki, avatar, cover); never strays.
SIDECAR = {"artist.md", "wiki.md"}
FIELDS = ["ARTIST", "ALBUMARTIST", "ALBUM", "TITLE", "DATE", "TRACKNUMBER", "TOTALTRACKS", "DISCNUMBER", "TOTALDISCS"]
MP4MAP = {"ARTIST": "©ART", "ALBUMARTIST": "aART", "ALBUM": "©alb", "TITLE": "©nam", "DATE": "©day"}
MP4_TEXT = {"©ART", "aART", "©alb", "©nam", "©day", "©gen", "©wrt", "©cmt", "©lyr", "©too", "cprt", "©grp", "soar",
            "soaa", "soal", "sonm", "desc", "ldes", "purd", "catg", "keyw", "©enc"}
LYRIC_KEYS = ("©lyr", "lyrics", "unsyncedlyrics")
VARIOUS = "Various Artists"
DECISION_TABLES = ("ARTIST_CANON", "ALBUM_CANON", "OVERRIDES", "DATE_PICK", "DATE_FILL", "ALBUM_FILL", "TITLE_FIX",
                   "DELETE", "DELETE_ALBUMS", "MOVE")
SETTLE_SECONDS = 180   # a file written more recently than this is probably still downloading

FEAT_RE = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring)\.?\s+(.+?)\s*[\)\]]\s*", re.I)
FEAT_TAIL_RE = re.compile(r"\s+(?:feat|ft|featuring)\.?\s+(.+?)\s*$", re.I)
GUEST_RE = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\.?\s+[^\)\]]+[\)\]]", re.I)
EDIT_RE = re.compile(r"\s*[\(\[][^\)\]]*(official|audio|video|visualizer|lyric)[^\)\]]*[\)\]]", re.I)
EDITION_RE = re.compile(
    r"\s*[\(\[]?\s*(deluxe(?: edition| version)?|remaster(?:ed)?(?: \d{4})?|\d{4} remaster(?:ed)?|expanded(?: edition)?"
    r"|super deluxe(?: edition)?|remastered and expanded edition|bonus track version|special edition|anniversary edition)"
    r"\s*[\)\]]?\s*$", re.I)

APPROVED_TEMPLATE = '''# music-tidy: approved decisions for this library. Persistent and cumulative: every entry here
# was confirmed by the owner before being applied. Path-keyed entries refer to the path at the time
# of the decision; once the file has been moved they simply no longer match (harmless).
# Comment each entry with the date and the reason.

ARTIST_CANON = {}   # spelling variant -> canonical (ARTIST + ALBUMARTIST, primary artist only)
ALBUM_CANON = {}    # casefolded album title -> canonical title
OVERRIDES = {}      # relpath -> {FIELD: value}; '' deletes the field
DATE_PICK = {}      # album_key -> DATE to use when album siblings disagree
DATE_FILL = {}      # 'albumartist|album' (both via akey / album_key) -> DATE for albums with no DATE at all
ALBUM_FILL = {}     # relpath -> ALBUM for files that have none
TITLE_FIX = {}      # relpath -> TITLE
DELETE = []         # relpaths approved for deletion (duplicate edits, off-album copies)
DELETE_ALBUMS = []  # album titles (casefolded match) to delete entirely (playlist dumps)
MOVE = {}           # non-audio relpath -> new relpath (booklets, nfo, cover art into their album folder)
'''


class TidyError(Exception):
    """A precondition for apply() is not met; the message says which."""


# Reading #

def open_file(path):
    audio = mutagen.File(path)

    if audio is None:
        raise ValueError("unrecognised format")

    if isinstance(audio, mutagen.mp3.MP3) and audio.tags is None:
        audio.add_tags()

    return audio


def get_fields(audio):
    fields = {k: [] for k in FIELDS}

    if isinstance(audio, MP4):
        tags = audio.tags or {}

        for field, atom in MP4MAP.items():
            fields[field] = [str(x) for x in tags.get(atom, [])]

        if "trkn" in tags:
            number, total = tags["trkn"][0]
            fields["TRACKNUMBER"] = [str(number)] if number else []
            fields["TOTALTRACKS"] = [str(total)] if total else []

        if "disk" in tags:
            number, total = tags["disk"][0]
            fields["DISCNUMBER"] = [str(number)] if number else []
            fields["TOTALDISCS"] = [str(total)] if total else []
    elif isinstance(audio, mutagen.mp3.MP3):
        tags = audio.tags

        def text(frame):
            return [str(x) for x in tags[frame].text] if frame in tags else []

        fields["ARTIST"], fields["ALBUMARTIST"], fields["ALBUM"] = text("TPE1"), text("TPE2"), text("TALB")
        fields["TITLE"], fields["DATE"] = text("TIT2"), text("TDRC")

        for frame, number_field, total_field in (("TRCK", "TRACKNUMBER", "TOTALTRACKS"), ("TPOS", "DISCNUMBER", "TOTALDISCS")):
            value = text(frame)

            if value:
                parts = value[0].split("/")
                fields[number_field] = [parts[0]] if parts[0] else []
                fields[total_field] = [parts[1]] if len(parts) > 1 and parts[1] else []
    else:   # Vorbis comments (FLAC / Opus / Ogg)
        tags = audio.tags or {}
        lower = {k.lower(): list(v) for k, v in tags.items()}

        for field in FIELDS:
            fields[field] = lower.get(field.lower(), [])

        if not fields["TOTALTRACKS"]:
            fields["TOTALTRACKS"] = lower.get("tracktotal", [])

        if not fields["TOTALDISCS"]:
            fields["TOTALDISCS"] = lower.get("disctotal", [])

    return fields


def all_text_tags(audio):
    if isinstance(audio, MP4):
        for key, values in (audio.tags or {}).items():
            if key in MP4_TEXT or (key.startswith("----") and values and isinstance(values[0], MP4FreeForm)):
                for index, value in enumerate(values):
                    if isinstance(value, MP4FreeForm):
                        try:
                            yield key, index, bytes(value).decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    elif isinstance(value, str):
                        yield key, index, value
    elif isinstance(audio, mutagen.mp3.MP3):
        for key, frame in audio.tags.items():
            if hasattr(frame, "text"):
                for index, value in enumerate(frame.text):
                    if isinstance(value, str):
                        yield key, index, value
    else:
        for key, values in (audio.tags or {}).items():
            if key.lower() in ("metadata_block_picture", "coverart"):
                continue

            for index, value in enumerate(values):
                yield key, index, value


def is_canonical_key(key):
    return key in MP4MAP.values() or key in ("trkn", "disk") or key.lower() in [f.lower() for f in FIELDS] + ["tracktotal", "disctotal"]


def raw_tag_snapshot(audio):
    snapshot = {}

    if isinstance(audio, MP4):
        for key, values in (audio.tags or {}).items():
            out = []

            for value in values:
                if key == "covr":
                    out.append(f"<cover {len(value)} bytes>")
                elif isinstance(value, MP4FreeForm):
                    try:
                        out.append(bytes(value).decode("utf-8"))
                    except UnicodeDecodeError:
                        out.append(f"<binary {len(value)} bytes>")
                elif isinstance(value, tuple):
                    out.append(list(value))
                else:
                    out.append(value)

            snapshot[key] = out
    elif isinstance(audio, mutagen.mp3.MP3):
        for key, frame in audio.tags.items():
            snapshot[key] = [str(x) for x in frame.text] if hasattr(frame, "text") else [f"<{type(frame).__name__}>"]
    else:
        for key, values in (audio.tags or {}).items():
            snapshot[key] = [f"<picture {len(x)} chars>" for x in values] if key.lower() == "metadata_block_picture" else list(values)

    return snapshot


# Writing #

def set_field(audio, field, values):
    if isinstance(audio, MP4):
        tags = audio.tags

        if field in MP4MAP:
            atom = MP4MAP[field]

            if values:
                tags[atom] = values
            elif atom in tags:
                del tags[atom]
        else:
            atom = "trkn" if "TRACK" in field else "disk"
            number, total = tags[atom][0] if atom in tags else (0, 0)

            if field in ("TRACKNUMBER", "DISCNUMBER"):
                number = int(values[0]) if values else 0
            else:
                total = int(values[0]) if values else 0

            if number or total:
                tags[atom] = [(number, total)]
            elif atom in tags:
                del tags[atom]
    elif isinstance(audio, mutagen.mp3.MP3):
        tags = audio.tags
        simple = {"ARTIST": TPE1, "ALBUMARTIST": TPE2, "ALBUM": TALB, "TITLE": TIT2, "DATE": TDRC}

        if field in simple:
            frame = simple[field].__name__

            if values:
                tags[frame] = simple[field](encoding=3, text=values)
            elif frame in tags:
                del tags[frame]
        else:
            frame = "TRCK" if "TRACK" in field else "TPOS"
            current = str(tags[frame].text[0]) if frame in tags else ""
            number, _, total = current.partition("/")

            if field in ("TRACKNUMBER", "DISCNUMBER"):
                number = values[0] if values else ""
            else:
                total = values[0] if values else ""

            joined = number + ("/" + total if total else "")

            if joined:
                tags[frame] = (TRCK if frame == "TRCK" else TPOS)(encoding=3, text=[joined])
            elif frame in tags:
                del tags[frame]
    else:
        tags = audio.tags
        keys = list(tags.keys())

        for key in keys:
            if key.lower() == field.lower():
                del tags[key]

        twin = {"TOTALTRACKS": "tracktotal", "TOTALDISCS": "disctotal"}.get(field)

        if twin:
            for key in keys:
                if key.lower() == twin:
                    if values:
                        tags[key] = values
                    else:
                        del tags[key]

        if values:
            tags[field] = values


def set_raw_text(audio, key, index, value):
    if isinstance(audio, MP4):
        values = list(audio.tags[key])
        values[index] = MP4FreeForm(value.encode("utf-8")) if isinstance(values[index], MP4FreeForm) else value
        audio.tags[key] = values
    elif isinstance(audio, mutagen.mp3.MP3):
        audio.tags[key].text[index] = value
    else:
        values = list(audio.tags[key])
        values[index] = value
        audio.tags[key] = values


# Normalisation helpers #

def clean_ws(text):
    return re.sub(r"[ \t ]+", " ", text.replace("\r\n", "\n")).strip()


def akey(name):
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("’", "'").replace("‘", "'").replace("&", "and")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if text.startswith("the "):
        text = text[4:]

    return text


def split_feat(artist):
    match = FEAT_RE.search(artist) or FEAT_TAIL_RE.search(artist)

    if match:
        return clean_ws(artist[:match.start()]), clean_ws(match.group(1))

    return artist, None


def primary_artist(artist):
    return split_feat(artist)[0]


def album_key(album):
    key = akey(album)
    stripped = EDITION_RE.sub("", album)
    return akey(stripped) if stripped else key


def title_key(title):
    return akey(EDIT_RE.sub("", GUEST_RE.sub("", title)))


def sanitize(text):
    text = (text.replace("/", "-").replace("\\", "-").replace(":", " -").replace("|", "-")
            .replace("?", "").replace("*", "").replace('"', "'").replace("<", "").replace(">", ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "_"


def first(entry, field):
    return entry.new[field][0] if entry.new[field] else ""


DISC_RE = re.compile(r"\s*[\(\[]?\s*(?:disc|disk|cd)\s*\d+\s*[\)\]]?\s*$", re.IGNORECASE)
TITLE_SPLIT_RE = re.compile(r"\s+[-–—]\s+|:\s+|\s+/\s+")


def album_variants(files):
    """Album titles that read as a variant of another album by the same album artist: a box-set disc
    named 'Set - Album', 'Album (Disc 2)', 'Album: Bonus'. One suggestion per variant, ready to become
    an ALBUM_CANON line; the album with more files is the canonical one."""
    albums = collections.defaultdict(dict)   # artist key -> album key -> {"title", "artist", "files"}

    for entry in files:
        album = first(entry, "ALBUM")
        artist = first(entry, "ALBUMARTIST") or primary_artist(first(entry, "ARTIST"))

        if not album or not artist or akey(artist) == akey(VARIOUS):
            continue

        slot = albums[akey(artist)].setdefault(album_key(album), {"title": album, "artist": artist, "files": 0})
        slot["files"] += 1

    found = []

    for titles in albums.values():
        if len(titles) < 2:
            continue

        for key, info in titles.items():
            title = info["title"]
            parts = {album_key(p) for p in TITLE_SPLIT_RE.split(title) if p.strip()}
            parts.add(album_key(DISC_RE.sub("", title)))
            parts.discard(key)
            targets = [t for k, t in titles.items() if k in parts]

            if targets:
                target = max(targets, key=lambda t: t["files"])
                found.append({"artist": info["artist"], "variant": title, "canonical": target["title"],
                              "files": info["files"], "canonical_files": target["files"]})

    return sorted(found, key=lambda v: (v["artist"].casefold(), v["variant"].casefold()))


def album_variant_lines(variants):
    """The ALBUM_CANON entries for the report and for approved.py."""
    return [f"    {v['variant'].casefold()!r}: {v['canonical']!r},   # {v['artist']}, {v['files']} file(s)" for v in variants]


class Entry:
    """One audio file: original tags (f), planned tags (new), the changes between them, and its target path."""

    rel: str
    path: str
    ext: str
    audio: object
    f: dict
    new: dict
    dur: float
    changes: list
    target: str


def change(entry, field, new, rule):
    old = entry.new[field]

    if old != new:
        entry.changes.append((field, old, new, rule))
        entry.new[field] = new


def ident(entry):
    album_artist = first(entry, "ALBUMARTIST") or primary_artist(first(entry, "ARTIST"))
    return (akey(primary_artist(album_artist)), title_key(first(entry, "TITLE")))


def target_path(entry):
    album_artist = sanitize(first(entry, "ALBUMARTIST") or primary_artist(first(entry, "ARTIST")) or "Unknown Artist")
    album = sanitize(first(entry, "ALBUM") or "Unknown Album")
    title = sanitize(first(entry, "TITLE") or os.path.splitext(os.path.basename(entry.rel))[0])
    number = first(entry, "TRACKNUMBER")
    name = (f"{int(number):02d} - " if number.isdigit() else "") + title + entry.ext
    return os.path.join(album_artist, album, name)


def load_decisions(path):
    """Read approved.py into a dict of the ten decision tables (missing tables stay empty)."""
    decisions = {name: ([] if name in ("DELETE", "DELETE_ALBUMS") else {}) for name in DECISION_TABLES}

    if os.path.exists(path):
        namespace = {}
        exec(compile(open(path, encoding="utf-8").read(), path, "exec"), namespace)   # noqa: S102 - the user's own decisions file

        for name in DECISION_TABLES:
            if name in namespace:
                decisions[name] = namespace[name]

    return decisions


class Tidy:
    def __init__(self, root, settle_seconds=SETTLE_SECONDS):
        self.root = str(Path(root).expanduser())

        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"music directory {self.root} does not exist")

        self.work = os.path.join(self.root, ".tidy")
        os.makedirs(self.work, exist_ok=True)
        self.approved_path = os.path.join(self.work, "approved.py")
        self.report_path = os.path.join(self.work, "report.txt")
        self.plan_path = os.path.join(self.work, "plan.json")
        self.log_path = os.path.join(self.work, "tag_changes.log")
        self.settle_seconds = settle_seconds
        self.decisions = load_decisions(self.approved_path)

    def ensure_approved_file(self):
        if not os.path.exists(self.approved_path):
            with open(self.approved_path, "w", encoding="utf-8") as handle:
                handle.write(APPROVED_TEMPLATE)

    # Loading #

    def load_all(self):
        files, failed, stray = [], [], []

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                ext = os.path.splitext(name)[1].lower()

                if ext not in EXT:
                    if not name.startswith("."):
                        stray.append(os.path.relpath(path, self.root))

                    continue

                entry = Entry()
                entry.rel = os.path.relpath(path, self.root)
                entry.path = path
                entry.changes = []
                entry.ext = ext

                try:
                    entry.audio = open_file(path)
                    entry.f = get_fields(entry.audio)
                    entry.dur = entry.audio.info.length
                except Exception as error:   # noqa: BLE001 - any unreadable file is reported, not fatal
                    failed.append((entry.rel, repr(error)))
                    continue

                entry.new = {k: list(v) for k, v in entry.f.items()}
                files.append(entry)

        return files, failed, stray

    # Tag plan #

    def build_plan(self, files):
        d = self.decisions
        notes = []

        # R1 whitespace on canonical fields
        for entry in files:
            for field in FIELDS:
                values = [x for x in (clean_ws(x) for x in entry.new[field]) if x != ""]
                change(entry, field, values, "R1 trim whitespace")

        # R2a strip ", <album>" suffix that YouTube-style tagging appended to ARTIST
        for entry in files:
            if entry.new["ARTIST"] and entry.new["ALBUM"]:
                artist, album = entry.new["ARTIST"][0], entry.new["ALBUM"][0]
                suffix = ", " + album

                if artist.casefold().endswith(suffix.casefold()) and len(artist) > len(suffix):
                    change(entry, "ARTIST", [artist[:-len(suffix)].rstrip(" ,")], "R2a drop album name appended to ARTIST")

        # R2b feat. normalisation in ARTIST; move feat from TITLE into ARTIST when ARTIST already names them
        for entry in files:
            if not entry.new["ARTIST"]:
                continue

            artist = entry.new["ARTIST"][0]
            base, feat = split_feat(artist)

            if feat:
                change(entry, "ARTIST", [f"{base} feat. {feat}"], "R2b normalise feat. form in ARTIST")
                continue

            if entry.new["TITLE"]:
                title = entry.new["TITLE"][0]
                match = FEAT_RE.search(title)

                if match:
                    featured = match.group(1)
                    names = [n for n in re.split(r"\s*(?:,|&|\band\b)\s*", featured) if n]

                    if all(n.casefold() in artist.casefold() for n in names) and any(n.casefold() != artist.casefold() for n in names):
                        primary = artist

                        for name in names:
                            primary = re.sub(r"\s*(?:,|&|\band\b)\s*" + re.escape(name) + r"\s*$", "", primary, flags=re.I)

                        primary = primary.rstrip(" ,&")
                        change(entry, "ARTIST", [f"{primary} feat. {featured}"], "R2b move feat. from TITLE into ARTIST")
                        change(entry, "TITLE", [clean_ws(title[:match.start()] + " " + title[match.end():])],
                               "R2b move feat. from TITLE into ARTIST")

        # snapshot for the variant report: after junk removal, before canonical spelling
        artist_groups = collections.defaultdict(collections.Counter)

        for entry in files:
            for field in ("ARTIST", "ALBUMARTIST"):
                for value in entry.new[field]:
                    artist_groups[akey(primary_artist(value))][primary_artist(value)] += 1

        # R2c canonical artist spelling (approved), applied to ARTIST and ALBUMARTIST
        for entry in files:
            for field in ("ARTIST", "ALBUMARTIST"):
                if entry.new[field]:
                    base, feat = split_feat(entry.new[field][0])
                    canonical = d["ARTIST_CANON"].get(base, base)

                    if canonical != base:
                        change(entry, field, [canonical + (f" feat. {feat}" if feat else "")], "R2c canonical artist spelling")

        # R4 album title fixes (approved)
        for entry in files:
            if entry.new["ALBUM"]:
                album = entry.new["ALBUM"][0]
                canonical = d["ALBUM_CANON"].get(album.casefold(), album)

                if canonical != album:
                    change(entry, "ALBUM", [canonical], "R4 album title canonical form")

        # R7 track/disc numbers as plain ints, totals split out
        for entry in files:
            for number_field, total_field in (("TRACKNUMBER", "TOTALTRACKS"), ("DISCNUMBER", "TOTALDISCS")):
                if entry.new[number_field]:
                    match = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?\s*$", entry.new[number_field][0])

                    if match:
                        change(entry, number_field, [str(int(match.group(1)))], "R7 plain integer track/disc number")

                        if match.group(2) and not entry.new[total_field]:
                            change(entry, total_field, [str(int(match.group(2)))], "R7 total moved to TOTAL* field")

                if entry.new[total_field] and re.match(r"^\d+$", entry.new[total_field][0]):
                    change(entry, total_field, [str(int(entry.new[total_field][0]))], "R7 plain integer total")

        # R6 DATE format normalisation
        for entry in files:
            if entry.new["DATE"]:
                value = entry.new["DATE"][0]
                match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:[T ].*)?$", value)

                if match:
                    change(entry, "DATE", [match.group(1) + (f"-{match.group(2)}-{match.group(3)}" if match.group(3) else "")],
                           "R6 DATE as YYYY or YYYY-MM-DD")
                else:
                    notes.append(f"unparseable DATE {value!r} in {entry.rel}")

        # R0 per-file manual overrides; R11 multi-value ARTIST; R12 album fill; R13 title fix (all approved)
        for entry in files:
            for field, value in d["OVERRIDES"].get(entry.rel, {}).items():
                change(entry, field, [value] if value else [], "R0 manual override")

            if len(entry.new["ARTIST"]) > 1:
                change(entry, "ARTIST", [entry.new["ARTIST"][0]], "R11 multi-value ARTIST -> primary artist (guests remain in TITLE)")

            if entry.rel in d["ALBUM_FILL"] and not entry.new["ALBUM"]:
                change(entry, "ALBUM", [d["ALBUM_FILL"][entry.rel]], "R12 fill missing ALBUM")

            if entry.rel in d["TITLE_FIX"]:
                change(entry, "TITLE", [d["TITLE_FIX"][entry.rel]], "R13 title typo")

        # album groups: (folder, album key)
        album_groups = collections.OrderedDict()

        for entry in files:
            album = entry.new["ALBUM"][0] if entry.new["ALBUM"] else ""
            key = (os.path.dirname(entry.rel), album_key(album) if album else "<no album>/" + entry.rel)
            album_groups.setdefault(key, []).append(entry)

        # R3 ALBUMARTIST on every file; R4 unify album title form within group
        for key, group in album_groups.items():
            existing = {e.new["ALBUMARTIST"][0] for e in group if e.new["ALBUMARTIST"]}
            primaries = collections.Counter(primary_artist(e.new["ARTIST"][0]) for e in group if e.new["ARTIST"])
            primary_keys = {akey(p) for p in primaries}

            if len(existing) == 1:
                album_artist = primary_artist(next(iter(existing)))
            elif len(primary_keys) == 1:
                album_artist = primaries.most_common(1)[0][0]
            else:
                album_artist = VARIOUS

            if len(primary_keys) == 1:
                sole = primaries.most_common(1)[0][0]

                if akey(album_artist) != akey(sole) and akey(sole) in akey(album_artist):
                    album_artist = sole

            forms = collections.Counter(e.new["ALBUM"][0] for e in group if e.new["ALBUM"])

            if len(forms) > 1:
                best = forms.most_common(1)[0][0]

                for entry in group:
                    if entry.new["ALBUM"] and entry.new["ALBUM"][0] != best:
                        change(entry, "ALBUM", [best], "R4 unify album title form within album")

            for entry in group:
                change(entry, "ALBUMARTIST", [album_artist], "R3 set ALBUMARTIST")

        # R6b DATE consistency / R4 title form across folders for the same (ALBUMARTIST, album)
        xgroups = collections.defaultdict(list)

        for entry in files:
            if entry.new["ALBUM"] and entry.new["ALBUMARTIST"]:
                xgroups[(akey(entry.new["ALBUMARTIST"][0]), album_key(entry.new["ALBUM"][0]))].append(entry)

        for key, group in xgroups.items():
            forms = collections.Counter(e.new["ALBUM"][0] for e in group)

            if len(forms) > 1:
                best = forms.most_common(1)[0][0]

                for entry in group:
                    if entry.new["ALBUM"][0] != best:
                        change(entry, "ALBUM", [best], "R4 unify album title form within album")

            dates = collections.Counter(e.new["DATE"][0] for e in group if e.new["DATE"])

            if not dates:
                date = d["DATE_FILL"].get(f"{key[0]}|{key[1]}")

                if not date:
                    continue

                for entry in group:
                    change(entry, "DATE", [date], "R6c DATE filled from approved lookup")

                continue

            date = d["DATE_PICK"].get(key[1]) or sorted(dates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

            for entry in group:
                if entry.new["DATE"] != [date]:
                    change(entry, "DATE", [date],
                           "R6 DATE made consistent within album" if entry.new["DATE"] else "R6 DATE propagated from album siblings")

        return artist_groups, album_groups, xgroups, notes

    # File plan: deletions, moves, strays #

    def is_download_report(self, rel):
        if not rel.lower().endswith(".txt"):
            return False

        try:
            with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as handle:
                return handle.readline().startswith("Download Report")
        except OSError:
            return False

    def failed_downloads(self, rel):
        out = []

        with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()

        for index, line in enumerate(lines):
            match = re.match(r"^\s*\d+\.\s+(.+)$", line)

            if match and index + 1 < len(lines) and "Error" in lines[index + 1]:
                out.append((match.group(1).strip(), lines[index + 1].strip()[:90]))

        return out

    def build_file_plan(self, files, stray):
        d = self.decisions
        plan = {}

        # R10 lossy copy of an existing FLAC (same album artist + title) -> delete
        flacs = collections.defaultdict(list)

        for entry in files:
            if entry.ext in LOSSLESS:
                flacs[ident(entry)].append(entry)

        deletions = []   # (entry, reason, rule)

        for entry in files:
            if entry.ext in LOSSLESS:
                continue

            key = ident(entry)

            if key in flacs:
                best = min(flacs[key], key=lambda other: abs(other.dur - entry.dur))
                note = " (music-video-length rip)" if abs(best.dur - entry.dur) > 5.0 else ""
                deletions.append((entry, f"lossy duplicate of FLAC {best.rel!r} ({entry.dur:.1f}s vs {best.dur:.1f}s){note}",
                                  "R10 flac beats lossy"))

        # approved deletions
        deleted = {entry for entry, _, _ in deletions}
        albums_to_delete = {a.casefold() for a in d["DELETE_ALBUMS"]}

        for entry in files:
            if entry in deleted:
                continue

            if entry.rel in d["DELETE"]:
                deletions.append((entry, "approved deletion (DELETE)", "R8 dedupe"))
                deleted.add(entry)
            elif first(entry, "ALBUM").casefold() in albums_to_delete:
                deletions.append((entry, f"album {first(entry, 'ALBUM')!r} removed on request", "R10 delete playlist-albums"))
                deleted.add(entry)

        keep = [entry for entry in files if entry not in deleted]

        # R14 targets
        clash = collections.defaultdict(list)

        for entry in keep:
            entry.target = target_path(entry)
            clash[entry.target.casefold()].append(entry)

        plan["clashes"] = {k: v for k, v in clash.items() if len(v) > 1}
        plan["moves"] = [entry for entry in keep if entry.rel != entry.target]
        plan["deletions"] = deletions
        plan["keep"] = keep

        # info: possible duplicate edits (same artist + title, not caught above)
        duplicates = collections.defaultdict(list)

        for entry in keep:
            if entry.new["TITLE"] and entry.new["ARTIST"]:
                duplicates[ident(entry)].append(entry)

        plan["dup_edits"] = [v for k, v in sorted(duplicates.items()) if len(v) > 1]

        # strays: download reports go to .tidy/reports; approved MOVE entries are filed; the rest is listed
        plan["reports"] = [s for s in stray if self.is_download_report(s)]
        plan["stray_moves"] = [(s, d["MOVE"][s]) for s in stray if s in d["MOVE"]]
        handled = set(plan["reports"]) | {s for s, _ in plan["stray_moves"]}

        # album extras (booklet, nfo) already sitting inside an Artist/Album folder are where they belong
        def is_extra(rel):
            return os.path.splitext(rel)[1].lower() in EXTRA and rel.count(os.sep) == 2

        plan["stray"] = [s for s in stray if s not in handled and os.path.splitext(s)[1].lower() not in IMG and not is_extra(s)
                         and os.path.basename(s) not in SIDECAR]
        plan["images"] = [s for s in stray if s not in handled and os.path.splitext(s)[1].lower() in IMG]

        # files still being written (downloader in flight)
        now = datetime.datetime.now().timestamp()
        plan["recent"] = [entry.rel for entry in files if now - os.path.getmtime(entry.path) < self.settle_seconds]
        return plan

    # Report #

    def write_report(self, files, failed, artist_groups, album_groups, notes, plan, out):
        d = self.decisions

        def P(*args):
            print(*args, file=out)

        P(f"# music-tidy dry run  {datetime.datetime.now():%Y-%m-%d %H:%M}   root={self.root}   files={len(files)} unreadable={len(failed)}\n")

        if plan["recent"]:
            P(f"!! {len(plan['recent'])} audio files written in the last {self.settle_seconds}s: a download may be in progress; "
              "apply will refuse until they settle:")

            for rel in plan["recent"]:
                P(f"     {rel}")

            P()

        if failed:
            P("## Unreadable files (skipped)")

            for rel, error in failed:
                P(f"  {rel}: {error}")

            P()

        P("## 1. Artist names with more than one spelling (ARTIST + ALBUMARTIST): needs an ARTIST_CANON decision")
        count = 0

        for key, counter in sorted(artist_groups.items()):
            if len(counter) > 1:
                count += 1
                P(f"  [{key}]")

                for value, n in counter.most_common():
                    P(f"      {n:4d}  {value!r}")

        P(f"  -> {count} groups   (ARTIST_CANON now: {json.dumps(d['ARTIST_CANON'], ensure_ascii=False)})\n")
        P("## 1b. ARTIST values carrying the ALBUM name appended (YouTube-style): stripped")

        for entry in files:
            for field, old, new, rule in entry.changes:
                if rule.startswith("R2a"):
                    P(f"  {old[0]!r:55s} -> {new[0]!r}   ({entry.rel})")

        P("\n## 1c. feat. handling")

        for entry in files:
            for field, old, new, rule in entry.changes:
                if rule.startswith("R2b"):
                    P(f"  {field:6s} {old} -> {new}   ({entry.rel})")

        P("\n## 1d. Album titles that read as a variant of another album by the same artist: needs an ALBUM_CANON decision")
        variants = album_variants(files)

        for line in album_variant_lines(variants):
            P(line)

        P(f"  -> {len(variants)} suggested   (paste into ALBUM_CANON, or `flacli tidy --accept-album-variants`)")

        P("\n## 2. Split albums (same folder + album identity, differing fields in the ORIGINAL tags)")
        count = 0

        for key, group in album_groups.items():
            if len(group) < 2:
                continue

            diff = {}

            for field in ("ALBUMARTIST", "ALBUM", "DATE", "DISCNUMBER", "TOTALTRACKS", "TOTALDISCS"):
                values = collections.Counter(json.dumps(e.f[field], ensure_ascii=False) for e in group)

                if len(values) > 1:
                    diff[field] = values

            effective = collections.Counter((e.f["ALBUMARTIST"] or e.f["ARTIST"] or ["<none>"])[0] for e in group)
            multi = any(len(e.f[k]) > 1 for e in group for k in ("ARTIST", "ALBUMARTIST", "ALBUM"))

            if not diff and len(effective) == 1 and not multi:
                continue

            count += 1
            P(f"  [{key[0] or '.'}] {key[1]!r}  ({len(group)} files)")

            if len(effective) > 1:
                P(f"      effective album-artist splits into {len(effective)}: "
                  + "; ".join(f"{v!r}x{c}" for v, c in effective.most_common()))

            for field, values in diff.items():
                P(f"      {field}: " + "; ".join(f"{v}x{c}" for v, c in values.most_common()))

            if multi:
                P("      multi-value tags present")

            P(f"      => proposed: ALBUMARTIST={first(group[0], 'ALBUMARTIST')!r}  ALBUM={first(group[0], 'ALBUM')!r}  "
              f"DATE={first(group[0], 'DATE') or '(none)'}")

        P(f"  -> {count} split albums\n")
        P("## 3. Missing fields (original tags): ALBUM needs an ALBUM_FILL decision")

        for field in ("ALBUMARTIST", "ALBUM", "ARTIST", "TRACKNUMBER", "DATE"):
            missing = [e.rel for e in files if not e.f[field]]
            P(f"  {field}: {len(missing)} files missing")

            if field in ("ALBUM", "ARTIST") or (field == "TRACKNUMBER" and len(missing) <= 60):
                for rel in missing:
                    P(f"      {rel}")

        P("\n## 4. Proposed ALBUMARTIST per album group  (Various = playlist-dump candidate; DELETE_ALBUMS if unwanted)")

        for key, group in album_groups.items():
            album_artist = first(group[0], "ALBUMARTIST")
            artists = collections.Counter(first(e, "ARTIST") for e in group if e.new["ARTIST"])
            album = first(group[0], "ALBUM") or "<no album>"
            P(f"  {album!r:60s} [{key[0] or '.'}] {len(group):3d} files  -> {album_artist!r}"
              f"{'  <-- Various' if album_artist == VARIOUS else ''}")

            if album_artist == VARIOUS or len(artists) > 1:
                P("        artists: " + "; ".join(f"{a}x{c}" for a, c in artists.most_common(8)) + (" ..." if len(artists) > 8 else ""))

        P("\n## 4b. Album title fixes in force (ALBUM_CANON)")

        for key, value in d["ALBUM_CANON"].items():
            P(f"  {key!r} -> {value!r}")

        P("\n## 5. Deletions")

        for entry, why, rule in plan["deletions"]:
            P(f"  DEL {entry.rel!r}   {why}   [{rule}]")

        P(f"  -> {len(plan['deletions'])} files")
        P("\n## 5b. Same artist + title more than once after deletions (duplicate edits? info only; add to DELETE to remove)")

        for group in plan["dup_edits"]:
            P("  " + " | ".join(f"{e.rel} ({e.dur:.0f}s)" for e in group))

        P("\n## 6. Moves  (Artist/Album/NN - Title.ext)")

        if plan["clashes"]:
            P("  !! PATH CLASHES: apply will abort until these are resolved:")

            for key, group in plan["clashes"].items():
                for entry in group:
                    P(f"     {entry.rel}  ->  {entry.target}")

        for entry in sorted(plan["moves"], key=lambda e: e.target):
            P(f"  {entry.rel[:70]:70s} -> {entry.target}")

        P(f"  -> {len(plan['moves'])} of {len(plan['keep'])} files")
        P(f"  files still without ALBUM: {sum(1 for e in plan['keep'] if not e.new['ALBUM'])}   "
          f"without TRACKNUMBER (no NN prefix): {sum(1 for e in plan['keep'] if not e.new['TRACKNUMBER'])}")
        P("\n## 7. Non-audio files")

        for rel in plan["reports"]:
            P(f"  REPORT {rel!r} -> .tidy/reports/")

            for what, error in self.failed_downloads(rel):
                P(f"         failed download: {what}   ({error})")

        for rel, dest in plan["stray_moves"]:
            P(f"  FILE   {rel!r} -> {dest!r}   [MOVE]")

        for rel in plan["stray"]:
            P(f"  STRAY  {rel!r}   (left alone; add to MOVE, or delete by hand)")

        if plan["images"]:
            P(f"  cover images left in place: {len(plan['images'])}")

        raw = sum(1 for e in files for key, index, value in all_text_tags(e.audio)
                  if not is_canonical_key(key) and key.lower() not in LYRIC_KEYS and clean_ws(value) != value)
        P(f"\n  other text tags (comments/freeform) with stray whitespace to trim: {raw}")
        P("\n## 8. Change counts per rule")
        rule_fields, rule_files = self.rule_counts(files)

        for rule, n in sorted(rule_fields.items()):
            P(f"  {rule:60s} {n:4d} field changes in {rule_files[rule]:4d} files")

        P(f"  files with any tag change: {sum(1 for e in files if e.changes)} / {len(files)}   "
          f"deletions: {len(plan['deletions'])}   moves: {len(plan['moves'])}")

        if notes:
            P("\n## Notes")

            for note in notes:
                P("  " + note)

    @staticmethod
    def rule_counts(files):
        rule_fields = collections.Counter()
        rule_files = collections.Counter()

        for entry in files:
            seen = set()

            for field, old, new, rule in entry.changes:
                rule_fields[rule] += 1

                if rule not in seen:
                    rule_files[rule] += 1
                    seen.add(rule)

        return rule_fields, rule_files

    # Entry points #

    def plan_everything(self):
        files, failed, stray = self.load_all()
        artist_groups, album_groups, xgroups, notes = self.build_plan(files)
        plan = self.build_file_plan(files, stray)
        return files, failed, artist_groups, album_groups, notes, plan

    def summary(self, files, failed, artist_groups, album_groups, notes, plan):
        """Compact numbers for the conversation; the report file carries the detail."""
        rule_fields, rule_files = self.rule_counts(files)
        artist_variants = sum(1 for counter in artist_groups.values() if len(counter) > 1)
        various = [first(group[0], "ALBUM") or "<no album>" for group in album_groups.values() if first(group[0], "ALBUMARTIST") == VARIOUS]
        missing_album = [e.rel for e in files if not e.f["ALBUM"]]
        failed_downloads = [{"report": rel, "item": what, "error": error} for rel in plan["reports"] for what, error in self.failed_downloads(rel)]
        return {
            "root": self.root,
            "files": len(files),
            "unreadable": [{"path": rel, "error": error} for rel, error in failed],
            "recent_files": plan["recent"],
            "open_questions": {
                "artist_spelling_variants": artist_variants,
                "album_title_variants": album_variants(files),
                "missing_album": len(missing_album),
                "various_artists_groups": various,
                "duplicate_edit_groups": len(plan["dup_edits"]),
                "strays": plan["stray"],
                "path_clashes": len(plan["clashes"]),
                "unparseable_dates": len(notes),
            },
            "tag_changes": {"files": sum(1 for e in files if e.changes),
                            "by_rule": {rule: {"fields": rule_fields[rule], "files": rule_files[rule]} for rule in sorted(rule_fields)}},
            "deletions": [{"path": e.rel, "why": why, "rule": rule} for e, why, rule in plan["deletions"]],
            "moves": len(plan["moves"]),
            "extras_to_file": len(plan["stray_moves"]),
            "download_reports": len(plan["reports"]),
            "failed_downloads": failed_downloads,
            "report_path": self.report_path,
            "plan_path": self.plan_path,
            "approved_path": self.approved_path,
        }

    def analyse(self):
        self.ensure_approved_file()
        files, failed, artist_groups, album_groups, notes, plan = self.plan_everything()

        with open(self.report_path, "w", encoding="utf-8") as handle:
            self.write_report(files, failed, artist_groups, album_groups, notes, plan, handle)

        with open(self.plan_path, "w", encoding="utf-8") as handle:
            json.dump({e.rel: [(field, old, new, rule) for field, old, new, rule in e.changes] for e in files if e.changes},
                      handle, indent=1, ensure_ascii=False)

        return self.summary(files, failed, artist_groups, album_groups, notes, plan)

    @staticmethod
    def _write_tags(entry, log, rule_fields, rule_files, errors) -> int:
        """Write an entry's planned tag changes (plus R1 on every other text tag); 1 when the file was saved."""
        # R1 on every other text tag (comments, freeform, ...); lyrics left alone
        for key, index, value in list(all_text_tags(entry.audio)):
            if key.lower() in LYRIC_KEYS or is_canonical_key(key):
                continue

            cleaned = clean_ws(value)

            if cleaned != value:
                set_raw_text(entry.audio, key, index, cleaned)
                entry.changes.append((key, [value[:60]], [cleaned[:60]], "R1 trim whitespace (other text tag)"))

        if not entry.changes:
            return 0

        seen = set()

        for field, old, new, rule in entry.changes:
            if field in FIELDS:
                set_field(entry.audio, field, new)

            log(entry.rel, field, f"{json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}", rule)
            rule_fields[rule] += 1

            if rule not in seen:
                rule_files[rule] += 1
                seen.add(rule)

        try:
            entry.audio.save()
            return 1
        except Exception as error:   # noqa: BLE001 - one bad file must not stop the run; it is reported
            errors.append((entry.rel, repr(error)))
            log(entry.rel, "SAVE FAILED", repr(error), "!!")
            return 0

    def _file_reports(self, plan, stamp, log):
        if not plan["reports"]:
            return

        os.makedirs(os.path.join(self.work, "reports"), exist_ok=True)

        for rel in plan["reports"]:
            dest = os.path.join(self.work, "reports", f"{stamp}_{os.path.basename(rel)}")
            os.rename(os.path.join(self.root, rel), dest)
            log(rel, "MOVED", os.path.relpath(dest, self.root), "R15 download report out of library")

    def _append_log(self, header, log_lines):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"# {header}\n" + "".join(line + "\n" for line in log_lines))

    def accept_album_variants(self):
        """Append every suggested album-title merge (report section 1d) to approved.py as ALBUM_CANON entries.
        The next analyse turns them into R4 tag changes and moves; nothing in the library changes here."""
        files, failed, artist_groups, album_groups, notes, plan = self.plan_everything()
        variants = album_variants(files)

        if variants:
            lines = [
                "",
                f"# {datetime.date.today().isoformat()} album titles that read as a variant of another album by the same artist "
                "(accepted from tidy's section 1d)",
                "ALBUM_CANON = globals().get('ALBUM_CANON', {})",
                "ALBUM_CANON.update({",
                *album_variant_lines(variants),
                "})",
                "",
            ]

            with open(self.approved_path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines))

        return {"accepted": variants, "approved_path": self.approved_path}

    def apply(self, force=False):
        files, failed, artist_groups, album_groups, notes, plan = self.plan_everything()

        if failed:
            raise TidyError("unreadable files present; fix or remove them first: " + "; ".join(rel for rel, _ in failed))

        if plan["clashes"]:
            raise TidyError("path clashes; resolve them in approved.py first (see report section 6)")

        if plan["recent"] and not force:
            raise TidyError(f"{len(plan['recent'])} audio files written in the last {self.settle_seconds}s; "
                            "a download may be in progress. Wait, or pass force=True.")

        stamp = f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}"
        os.makedirs(os.path.join(self.work, "backups"), exist_ok=True)
        backup_path = os.path.join(self.work, "backups", f"tags_{stamp}.json")

        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump({e.rel: raw_tag_snapshot(e.audio) for e in files}, handle, indent=1, ensure_ascii=False)

        log_lines = []

        def log(rel, action, detail, rule):
            log_lines.append(f"{rel}\t{action}\t{detail}\t[{rule}]")

        errors = []

        # deletions
        for entry, why, rule in plan["deletions"]:
            os.remove(entry.path)
            log(entry.rel, "DELETED", why, rule)

        # tags
        rule_fields = collections.Counter()
        rule_files = collections.Counter()
        written = 0

        for entry in plan["keep"]:
            written += self._write_tags(entry, log, rule_fields, rule_files, errors)

        # moves
        moved = 0

        for entry in plan["moves"]:
            dest = os.path.join(self.root, entry.target)
            os.makedirs(os.path.dirname(dest), exist_ok=True)

            if os.path.exists(dest):
                errors.append((entry.rel, f"target exists: {entry.target}"))
                log(entry.rel, "MOVE FAILED", entry.target, "!!")
                continue

            os.rename(entry.path, dest)
            moved += 1
            log(entry.rel, "MOVED", entry.target, "R14 Artist/Album/NN - Title")

        # download reports out of the library
        self._file_reports(plan, stamp, log)

        # approved non-audio moves (booklets, nfo, cover art into album folders)
        filed = 0

        for rel, target in plan["stray_moves"]:
            dest = os.path.join(self.root, target)

            if os.path.exists(dest):
                errors.append((rel, f"target exists: {target}"))
                log(rel, "MOVE FAILED", target, "!!")
                continue

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(os.path.join(self.root, rel), dest)
            filed += 1
            log(rel, "MOVED", target, "R17 album extra filed with its album")

        # prune empty dirs
        pruned = 0

        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            if dirpath == self.root or os.path.basename(dirpath).startswith(".") or dirpath.startswith(self.work):
                continue

            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                pruned += 1
                log(os.path.relpath(dirpath, self.root), "RMDIR", "empty", "R16 prune")

        self._append_log(f"apply {stamp}", log_lines)

        return {
            "root": self.root,
            "deleted": len(plan["deletions"]),
            "retagged": written,
            "moved": moved,
            "extras_filed": filed,
            "reports_filed": len(plan["reports"]),
            "pruned_folders": pruned,
            "errors": [{"path": rel, "error": error} for rel, error in errors],
            "by_rule": {rule: {"fields": rule_fields[rule], "files": rule_files[rule]} for rule in sorted(rule_fields)},
            "backup_path": backup_path,
            "log_path": self.log_path,
        }

    # Scoped pass for files that have just arrived #

    def apply_new(self, paths):
        """Tidy only the given (just-downloaded) audio files: retag them and file them as Artist/Album/NN - Title.

        Everything else in the library is left exactly as it is: no deletions, no sibling propagation, no settle
        check (the caller knows these files are complete). A file is held in place, and reported, when the full
        tidy would delete it (lossy copy of an owned FLAC), when it lacks ARTIST, ALBUM or TITLE, or when its
        target already exists. Images and booklets left behind in an emptied download folder follow the album;
        the emptied folder is pruned. Download reports are filed as in the full tidy.
        """
        wanted, outside = set(), []

        for path in paths:
            full = os.path.abspath(os.path.expanduser(str(path)))

            if os.path.commonpath([full, self.root]) != self.root:
                outside.append(str(path))
                continue

            wanted.add(os.path.relpath(full, self.root))

        files, failed, artist_groups, album_groups, notes, plan = self.plan_everything()
        new = [entry for entry in files if entry.rel in wanted]
        known = {entry.rel for entry in files} | {rel for rel, _ in failed}
        held = [{"path": rel, "why": f"unreadable: {error}"} for rel, error in failed if rel in wanted]
        held += [{"path": rel, "why": "not found, or not an audio file"} for rel in sorted(wanted - known)]
        held += [{"path": path, "why": "outside the library"} for path in outside]
        deletions = {entry: why for entry, why, _ in plan["deletions"]}
        clashing = {entry for group in plan["clashes"].values() for entry in group}
        stamp = f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}"
        log_lines, errors = [], []
        rule_fields, rule_files = collections.Counter(), collections.Counter()

        def log(rel, action, detail, rule):
            log_lines.append(f"{rel}\t{action}\t{detail}\t[{rule}]")

        backup_path = None

        if new:
            os.makedirs(os.path.join(self.work, "backups"), exist_ok=True)
            backup_path = os.path.join(self.work, "backups", f"tags_{stamp}_new.json")

            with open(backup_path, "w", encoding="utf-8") as handle:
                json.dump({e.rel: raw_tag_snapshot(e.audio) for e in new}, handle, indent=1, ensure_ascii=False)

        written, moves, sources = 0, {}, collections.defaultdict(set)

        for entry in new:
            if entry in deletions:
                held.append({"path": entry.rel, "why": f"the full tidy would delete it: {deletions[entry]}"})
                continue

            written += self._write_tags(entry, log, rule_fields, rule_files, errors)

            if not (first(entry, "ALBUM") and first(entry, "TITLE") and (first(entry, "ALBUMARTIST") or first(entry, "ARTIST"))):
                held.append({"path": entry.rel, "why": "missing ARTIST, ALBUM or TITLE; settle it in approved.py and run the full tidy"})
                continue

            if entry in clashing:
                held.append({"path": entry.rel, "why": f"target path clash: {entry.target}"})
                continue

            if entry.rel == entry.target:
                continue

            dest = os.path.join(self.root, entry.target)

            if os.path.exists(dest):
                held.append({"path": entry.rel, "why": f"target exists: {entry.target}"})
                continue

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(entry.path, dest)
            moves[entry.path] = dest
            sources[os.path.dirname(entry.path)].add(os.path.dirname(dest))
            log(entry.rel, "MOVED", entry.target, "R14 Artist/Album/NN - Title")

        # emptied download folders: images and booklets follow the album, then the folder goes
        filed, pruned = 0, 0

        for source, targets in sources.items():
            if source == self.root or not os.path.isdir(source):
                continue

            leftovers = sorted(os.listdir(source))

            if any(os.path.splitext(name)[1].lower() in EXT for name in leftovers):
                continue   # more audio still to come (or to be decided on)

            if len(targets) == 1:
                target = next(iter(targets))

                for name in leftovers:
                    if os.path.splitext(name)[1].lower() not in IMG | EXTRA or os.path.exists(os.path.join(target, name)):
                        continue

                    os.rename(os.path.join(source, name), os.path.join(target, name))
                    filed += 1
                    log(os.path.relpath(os.path.join(source, name), self.root), "MOVED",
                        os.path.relpath(os.path.join(target, name), self.root), "R17 album extra filed with its album")

            folder = source

            while folder != self.root and os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)
                pruned += 1
                log(os.path.relpath(folder, self.root), "RMDIR", "empty", "R16 prune")
                folder = os.path.dirname(folder)

        self._file_reports(plan, stamp, log)

        if log_lines:
            self._append_log(f"apply-new {stamp}", log_lines)

        return {
            "root": self.root,
            "requested": len(paths),
            "retagged": written,
            "moved": len(moves),
            "moves": moves,
            "extras_filed": filed,
            "reports_filed": len(plan["reports"]),
            "pruned_folders": pruned,
            "held": held,
            "errors": [{"path": rel, "error": error} for rel, error in errors],
            "by_rule": {rule: {"fields": rule_fields[rule], "files": rule_files[rule]} for rule in sorted(rule_fields)},
            "deletions_waiting": len(plan["deletions"]),
            "backup_path": backup_path,
            "log_path": self.log_path,
        }


def main(argv=None):
    """``flacli tidy analyse|apply [--force] [root]``: the same planner from a shell."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    command = args[0] if args else "analyse"
    root = args[1] if len(args) > 1 else os.environ.get("MUSIC_ROOT") or os.environ.get("FLACLI_MUSIC_DIR") or "~/Music"

    try:
        tidy = Tidy(root)

        if command == "analyse":
            summary = tidy.analyse()
            print(open(tidy.report_path, encoding="utf-8").read())
            print(json.dumps({k: v for k, v in summary.items() if k in ("files", "deletions", "moves", "open_questions")},
                             indent=1, ensure_ascii=False))
        elif command == "apply":
            print(json.dumps(tidy.apply(force=force), indent=1, ensure_ascii=False))
        else:
            sys.exit("usage: flacli tidy analyse | apply [--force] [root]")
    except (TidyError, FileNotFoundError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
