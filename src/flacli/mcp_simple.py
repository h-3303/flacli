# SPDX-License-Identifier: GPL-3.0-or-later
"""The simple MCP server: fifteen coarse tools, each one whole step, for small or local models.

`flacli mcp` runs it over stdio. Capable models can use `flacli mcp --full` (51 fine-grained tools) instead.
"""

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import simple
from .bridge import BridgeError
from .connectors import ConnectorError
from .musicbrainz import MusicBrainzError
from .tidy import TidyError
from .wiki import WikiError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
NETWORK = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="flacli",
    instructions=(
        "flacli gets music onto disk through the user's own Nicotine+ (Soulseek) client. Everything is local. "
        "To fetch named songs or albums: get_music(items). To fetch a playlist: sync_playlist(target), then "
        "status(playlist_id) until the job is finished, then queue_downloads(playlist_id) to see the totals, show them to "
        "the user, and queue_downloads(playlist_id, yes=True) only after they agree. status() also follows the downloads "
        "and files finished tracks. review_candidates / approve_tracks / skip_tracks handle doubtful matches. write_m3u "
        "writes the playlist file. tidy_library(apply=False) plans a library clean-up; apply=True only after the user has "
        "seen the deletions. wiki_todo / wiki_fill / wiki_sources / wiki_write give artists and albums the bio and "
        "wiki text the player shows. doctor() when anything fails."
    ),
)


def tool_errors(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (LookupError, ValueError, FileNotFoundError, PermissionError, BridgeError, MusicBrainzError, TidyError,
                ConnectorError, OSError, WikiError) as error:
            raise ToolError(str(error)) from None

    return wrapper


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def doctor() -> dict:
    """Health check: is Nicotine+ reachable, does the music dir exist, what is configured. Call this first when
    something fails; the result says how to fix it."""
    return await simple.doctor()


@mcp.tool(annotations=NETWORK)
@tool_errors
async def get_music(items: list[str], lossy: bool = False, min_confidence: float = 0.85) -> dict:
    """Fetch named songs and albums in one call. Items: "Artist - Title" for a song, "Artist - Album (album)" for a
    whole album. Resolves them on MusicBrainz, skips what the library already has, and starts a background job that
    searches Soulseek and queues every match at or above min_confidence; the request itself is the go-ahead, no extra
    confirmation. Report what was understood, then call status(playlist_id) in a minute or two."""
    return await simple.get(items, lossy=lossy, min_confidence=min_confidence)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def sync_playlist(target: str, yes: bool = False, lossy: bool = False, min_confidence: float = 0.85) -> dict:
    """Import a playlist (file path: Spotify export, CSV, M3U, JSPF, XSPF; or a TIDAL / Deezer / YouTube Music share
    URL; or an existing playlist id), resolve it on MusicBrainz, diff it against the library, and match the missing
    tracks on Soulseek in the background. Nothing is downloaded unless yes=True, which the user must have asked for.
    Follow with status(playlist_id) until the job is finished, then queue_downloads."""
    return await simple.sync(target, yes=yes, lossy=lossy, min_confidence=min_confidence)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def status(playlist_id: int | None = None) -> dict:
    """All playlists, or one playlist: counts by state, the running or last job, download progress. Finished
    downloads are filed into the library (tags normalised, Artist/Album/NN - Title) as a side effect. The 'next'
    field says what to do next."""
    return await simple.status(playlist_id)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def review_candidates(playlist_id: int, doubtful_only: bool = True, status: str = "candidates", limit: int = 25, offset: int = 0) -> dict:
    """Tracks that need a decision with their best candidates: confidence, quality, why they scored, user queue.
    doubtful_only hides everything at or above 0.85. status="not_found" lists what Soulseek did not have."""
    return await simple.review(playlist_id, status_name=status, doubtful_only=doubtful_only, limit=limit, offset=offset)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def approve_tracks(playlist_id: int, track_ids: list[int] | None = None, min_confidence: float | None = None, candidate: int = 0) -> dict:
    """Approve candidates: give track_ids (candidate picks which one, 0 = best), or min_confidence to approve every
    track in the playlist at or above it. Approving queues nothing; queue_downloads does."""
    return await simple.approve(playlist_id, track_ids=track_ids, min_confidence=min_confidence, candidate=candidate)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def skip_tracks(track_ids: list[int], reason: str = "skipped by user") -> dict:
    """Leave these tracks out of matching and the M3U."""
    return await simple.skip(track_ids, reason=reason)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def queue_downloads(playlist_id: int, yes: bool = False, min_confidence: float | None = None) -> dict:
    """Without yes: the totals (tracks, size, users) of what would be downloaded; show them to the user. With
    yes=True: queue the transfers in Nicotine+. min_confidence first approves every candidate at or above it."""
    return await simple.queue(playlist_id, yes=yes, min_confidence=min_confidence)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def write_m3u(playlist_id: int, path: str | None = None) -> dict:
    """Write the playlist as M3U8 in its original order from the local files; lists the tracks still missing."""
    return await simple.m3u(playlist_id, path=path)


@mcp.tool(annotations=DESTRUCTIVE)
@tool_errors
async def tidy_library(apply: bool = False, force: bool = False) -> dict:
    """Library clean-up: normalise tags, drop lossy duplicates of FLACs, file everything as Artist/Album/NN - Title.
    apply=False is a dry run that writes a report; apply=True changes files and must only follow the user's yes to
    the listed deletions."""
    return await simple.tidy(apply=apply, force=force)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def cancel_job(playlist_id: int) -> dict:
    """Stop the running matching job of a playlist."""
    return await simple.cancel(playlist_id)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def wiki_todo(include_all: bool = False) -> dict:
    """Artists and albums in the library that have no bio / wiki text yet, with what a writer needs (tracks, year,
    MusicBrainz ids). Start here, then wiki_fill; write the rest yourself with wiki_sources + wiki_write."""
    return await simple.wiki_missing(include_all=include_all)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def wiki_fill(limit: int = 10) -> dict:
    """Give every entry without text its English Wikipedia lead paragraph when MusicBrainz links one (attributed,
    CC BY-SA), `limit` entries per call. The rest come back in `to_write` with MusicBrainz facts and links: write
    those yourself, from the facts only, and store each with wiki_write. Call again while `remaining` > 0."""
    return await simple.wiki_fill(limit=limit)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def wiki_sources(artist: str, album: str | None = None) -> dict:
    """Facts and links for one artist (album=None) or album: MusicBrainz type, dates, area, labels, tags, annotation,
    outbound links (Wikidata, Discogs, Bandcamp, homepage), and the Wikipedia lead when there is an article."""
    return await simple.wiki_sources(artist, album)


@mcp.tool(annotations=LOCAL)
@tool_errors
async def wiki_write(artist: str, content: str, attribution: str, album: str | None = None, url: str | None = None,
                     force: bool = False) -> dict:
    """Store the text for an artist (album=None) or an album: written to a sidecar file beside the music and pushed
    into the player's cache. Plain text, no markup; one paragraph for an album, two for an artist; only what the
    sources support. attribution is shown under the text: name the sources and, if you wrote it, yourself
    (e.g. "Written by Claude from MusicBrainz and Discogs, 2026-09-17"). force replaces existing text."""
    return await simple.wiki_set(artist, content, attribution, album=album, url=url, force=force)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
