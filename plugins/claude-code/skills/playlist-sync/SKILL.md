---
name: playlist-sync
description: Import a playlist straight from TIDAL, Deezer or YouTube Music, or from an export file (Spotify data export, Exportify CSV, CSV, M3U, JSPF, XSPF), canonicalise it against MusicBrainz, find which tracks are already in the local library, fetch the missing ones through the running Nicotine+ on Soulseek with the user's approval, and write an M3U in the original order. Also the direct route when the user simply names songs or albums they want ("get me X by Y", "the whole of that album"): request_music fetches them in one call. Use when the user mentions a playlist file or link, a TIDAL/Deezer/YouTube Music playlist, syncing a playlist, missing tracks, or getting a song, an album or a tracklist onto disk.
tags: [music, soulseek, playlists]
---

# Playlist sync

Two MCP servers do the work: `flacli` (playlists, MusicBrainz, local library, matching state) and
`soulseek` (direct control of the running Nicotine+ client). Both are the `flacli` command, which the
user can also run in a shell; `flacli guide` is the short form of this skill. Everything is local; the only network
calls are MusicBrainz lookups and Soulseek traffic through the user's own Nicotine+.

## Direct requests (no playlist)

When the user names songs or albums rather than handing over a playlist, call `request_music(items)`
once with everything they named: `{"artist": ..., "title": ...}` per song, `{"artist": ..., "album": ...}`
per album (strings "Artist - Title" / "Artist - Album (album)" also work, and so does the dict as a JSON string). It appends them to the
persistent **Requests** playlist (a different `playlist=` name keeps things apart), expands albums to
their tracklist through MusicBrainz, checks the library, and starts one background job that matches the
rest and queues every match at or above `min_confidence` (0.85), whole folders for albums. This is the one
place downloads start without a separate confirmation: the request itself is the go-ahead, so do not ask
for one, but do report immediately what was understood: each album's chosen release (title, date, track
count), how many tracks were already owned, how many are being fetched, and anything unresolved. A wrong
release is cancelled with `cancel_job` plus the `soulseek` server's `cancel_downloads`. Then follow steps
8 and 9 below: poll `playlist_status` (the job's `phase` goes matching → queueing → finished, with
`queued`, `total_mb`, `users`, `for_review`, `not_found`), `sync_downloads` when the monitor says
transfers finished, and `review_candidates` for the tracks left `for_review`. An album MusicBrainz does
not know comes back with an error entry: ask for the tracklist, or search it with the `soulseek` server
by hand. If the user wants lossy files, pass `allow_formats` and `min_bitrate` exactly as for
`match_playlist`.

## Workflow

1. **Import.** From a file: `import_playlist_file(path)`; format is auto-detected. A Spotify export with
   several playlists imports them all — if that is not what the user wants, list the names and let them
   pick (`playlist_name`). From a service: `import_remote_playlist(service, id_or_url)` with a share link
   or id, or `list_remote_playlists(service)` first and let the user pick. Report the playlist_id, track
   count and any warnings. Services:
   - **tidal** — needs the user's own TIDAL app (`flacli config set tidal_client_id <id>`). `connect_service("tidal")`
     returns an `authorize_url` and tries to open it; tell the user to log in and approve, then call
     `connect_service("tidal")` again to finish. Tokens refresh themselves afterwards.
   - **deezer** — public playlists only, no login. `list_remote_playlists("deezer", user=<id or profile URL>)`.
   - **youtube-music** — unofficial (ytmusicapi). `service_status("youtube-music")` explains how to copy
     request headers from a logged-in tab; then `connect_service("youtube-music", headers_raw=...)`.
     Expect it to break occasionally when YouTube changes; say so rather than retrying.
   `service_status()` shows what is connected. `disconnect_service` forgets stored tokens.
2. **Resolve.** `resolve_playlist(playlist_id)`. Spotify exports and YouTube Music carry no ISRCs (and
   Spotify exports no durations); this fills in what it can. Runs at one request per second, so a 300-track playlist takes a few minutes — say so up front.
   Show the unresolved tracks; they can still be matched, just with less certainty.
3. **Library.** `scan_library()` once per session (incremental afterwards), then
   `diff_library(playlist_id)`. Report how many tracks are already owned. If `troi_status()` says Troi is
   installed and the collection is MusicBrainz-tagged, `troi_resolve(playlist_id)` afterwards catches
   tracks the plain diff missed (`troi_scan()` first, once, to index the collection).
4. **Match.** `match_playlist(playlist_id, ...)` starts a background job. Poll `playlist_status`
   every 20–30 s (each track needs a Soulseek search and ~10 s harvesting; a 100-track playlist is
   several minutes). If the status says it is waiting on the rate limit, wait — do not work around it.
5. **Review.** `review_candidates(playlist_id)`; start with `max_confidence=0.7` to look at the
   doubtful ones. Explain briefly why a candidate is doubtful (the `why` breakdown). For playlists over
   ~40 tracks, delegate the review to the `matcher` agent and act on its shortlist.
6. **Approve.** `approve(track_ids=[...])`, or `approve(playlist_id=..., min_confidence=0.85)` for the
   confident ones. `skip_tracks` for anything the user does not want.
7. **Queue.** Call `queue_approved(playlist_id)` **without** confirm, show the user the track count,
   total size and user list, and wait for their explicit OK. Only then `queue_approved(confirm=True)`.
8. **Sync.** `sync_downloads(playlist_id)` after a while (and again later); it maps transfers to
   done/failed and retries failed ones from the next candidate. Unless the `auto_tidy` setting is off (`flacli config`) it
   also files each track it marks done (tags normalised, moved to `Artist/Album/NN - Title`, cover art
   following, emptied download folder removed) and reports that under `tidied`: mention where the files
   went and relay any `held` entries (a file it would not place, with the reason). Use the `soulseek`
   server's `list_downloads` for detail. A download monitor starts with this skill and posts one line per finished
   or failed transfer, and one when nothing is left in progress: that is the cue to sync, not a reason to
   poll in a loop.
9. **Write.** `write_m3u(playlist_id)`. Tell the user which tracks are still missing.
10. **File.** With `auto_tidy` on (the default) the new tracks are already filed; offer `/music-tidy`
    only for what it reported as held or for the rest of the library. If `beets_status()` says beets is
    installed and the user prefers it, they should turn `auto_tidy` off; then offer `beets_import(playlist_id)`:
    show its dry-run plan (folders, files, commands, copy or move per the beets config) and only after a
    yes call `beets_import(playlist_id, confirm=True)`. Never run both beets and the tidy on the same
    folder.

## Guardrails

- Never call `queue_approved(confirm=True)` without first showing its totals and getting an explicit
  yes in the same conversation. `request_music` is the deliberate exception: it queues on its own
  because the user's request named exactly what to get; never route a playlist import through it to
  skip the confirmation.
- Prefer album mode (the default `album_mode="auto"`) when most of an album is missing; it fetches
  whole folders and checks the track count against MusicBrainz. Do not force `album_mode="off"`
  unless the user asks for single files.
- Never change the Nicotine+ plugin's search rate limit, and never suggest doing so, without asking:
  Soulseek bans the account for 30 minutes when it is exceeded.
- Do not dump whole tracklists into the conversation. Work with counts, ids and slices.
- Keep formats honest: prefer FLAC by default; if the user wants lossy, pass `allow_formats` and
  `min_bitrate` explicitly.
- If the bridge is unreachable, tell the user to start Nicotine+ and enable the MCP Bridge plugin
  (Preferences → Plugins). Do not retry in a loop.
- `beets_import(confirm=True)` and `tidy_apply(confirm=True)` both move files: the dry-run plan must be
  shown and approved in the same conversation first, every time.
- Service tokens and cookies stay in 0600 files under the flacli data dir. Never echo `headers_raw`,
  tokens or cookie values back into the conversation, and never write them anywhere else.
- Spotify's API is deliberately not connected (its developer terms forbid feeding API data to an AI
  model); the Spotify route is the data export or an Exportify CSV.
