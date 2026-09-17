# SPDX-License-Identifier: GPL-3.0-or-later
"""JSPF and XSPF playlists (XSPF is the XML form of the same model)."""

import json

from pathlib import Path
from xml.etree import ElementTree

from ..jspf import track_from_jspf
from ..models import ImportedPlaylist

XSPF_NS = "http://xspf.org/ns/0/"


def parse_jspf(path: Path) -> ImportedPlaylist:
    data = json.loads(path.read_text(encoding="utf-8"))
    playlist = data.get("playlist") if isinstance(data, dict) else None

    if not isinstance(playlist, dict):
        raise ValueError(f"{path.name} is not a JSPF file (missing 'playlist' object)")

    warnings: list[str] = []
    tracks = []

    for index, item in enumerate(playlist.get("track") or []):
        track = track_from_jspf(item, len(tracks))

        if not track.title:
            warnings.append(f"track {index + 1}: skipped entry without a title")
            continue

        tracks.append(track)

    if not tracks:
        raise ValueError(f"{path.name} contains no tracks")

    return ImportedPlaylist(name=playlist.get("title") or path.stem, source="jspf", tracks=tracks, warnings=warnings)


def _xspf_track_to_dict(element) -> dict:
    def text(tag):
        node = element.find(f"{{{XSPF_NS}}}{tag}")
        return node.text.strip() if node is not None and node.text else None

    item = {"title": text("title"), "creator": text("creator"), "album": text("album")}
    duration = text("duration")

    if duration and duration.isdigit():
        item["duration"] = int(duration)

    identifiers = [n.text.strip() for n in element.findall(f"{{{XSPF_NS}}}identifier") if n.text]

    if identifiers:
        item["identifier"] = identifiers

    locations = [n.text.strip() for n in element.findall(f"{{{XSPF_NS}}}location") if n.text]

    if locations:
        item["location"] = locations

    return item


def parse_xspf(path: Path) -> ImportedPlaylist:
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as error:
        raise ValueError(f"{path.name} is not well-formed XML: {error}") from None

    if root.tag != f"{{{XSPF_NS}}}playlist":
        raise ValueError(f"{path.name} is not an XSPF playlist")

    title_node = root.find(f"{{{XSPF_NS}}}title")
    name = title_node.text.strip() if title_node is not None and title_node.text else path.stem
    warnings: list[str] = []
    tracks = []

    for index, element in enumerate(root.iter(f"{{{XSPF_NS}}}track")):
        track = track_from_jspf(_xspf_track_to_dict(element), len(tracks))

        if not track.title:
            warnings.append(f"track {index + 1}: skipped entry without a title")
            continue

        tracks.append(track)

    if not tracks:
        raise ValueError(f"{path.name} contains no tracks")

    return ImportedPlaylist(name=name, source="xspf", tracks=tracks, warnings=warnings)
