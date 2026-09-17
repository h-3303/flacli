# flacli agent guide

flacli gets music onto disk. It takes named songs, albums or a playlist, canonicalises them on MusicBrainz,
skips what the local library already holds, finds the rest on Soulseek through the user's own running Nicotine+
client, files each track as `Artist/Album/NN - Title` when it lands, and writes an M3U. Everything runs locally.

Every command prints one JSON object. Read it; the `note` and `next` fields say what to do. An error prints
`{"error": "..."}` and exits 1. Long work runs in the background, so commands return within seconds; `flacli status`
is how you follow it.

## Quick reference

```
flacli doctor                         health check; run first when anything fails
flacli get "Artist - Title" ...       fetch songs; "Artist - Album (album)" fetches a whole album
flacli sync <file|url|id>             import + resolve + diff + match a playlist (nothing downloaded yet)
flacli sync <...> --yes               same, and queue every confident match (user asked for it)
flacli status [id]                    playlists, or one playlist's job, counts, downloads; files finished tracks
flacli review <id>                    tracks needing a decision, with candidates and reasons
flacli approve <id> --tracks 3,4      accept the best candidate (or --candidate N, or --min-confidence 0.85)
flacli skip <id> --tracks 5           leave tracks out
flacli queue <id>                     totals of what would download; show them to the user
flacli queue <id> --yes               queue it (only after the user said yes)
flacli m3u <id>                       write the playlist file; lists what is still missing
flacli tidy [--apply]                 library clean-up plan; apply only after the user saw the deletions
flacli search "query"                 raw Soulseek search when the matcher found nothing
flacli config set music_dir ~/Music   settings live in ~/.config/flacli/config.toml
flacli guide                          this text
```

## Workflow: the user names songs or albums

1. `flacli get "Lorde - Royals" "Boards of Canada - Geogaddi (album)"`. One call with everything named.
   It reports `understood` (for albums: the release picked and its track count), `already_in_library`, `to_fetch`,
   and `unresolved`. Tell the user this right away. The request itself is the go-ahead: confident matches are
   queued without asking again.
2. Wait a minute or two, then `flacli status <playlist_id>`. Repeat until `next` says it is done. Do not poll more
   than once a minute; each track needs a Soulseek search that collects results for ten seconds.
3. If `counts.candidates` is non-zero, `flacli review <id>` shows the doubtful ones. Decide with `approve` / `skip`,
   then `flacli queue <id>` and, on the user's yes, `flacli queue <id> --yes`.
4. `flacli status <id>` files each finished download into the library and reports `tidied`. Say where the files went.

## Workflow: the user hands over a playlist

1. `flacli sync <file or share URL>`. Files: Spotify data export, Exportify CSV, generic CSV, M3U, JSPF, XSPF.
   URLs: TIDAL, Deezer (public playlists), YouTube Music. TIDAL and YouTube Music need `flacli service connect`
   first; `flacli service status` explains. Resolving on MusicBrainz runs at one request per second, so a
   300-track playlist takes a few minutes; say so.
2. `flacli status <id>` until the job is finished.
3. `flacli queue <id>` shows the totals: track count, size, the users involved. Show them to the user and wait for
   an explicit yes. Then `flacli queue <id> --yes`. Never pass `--yes` without that yes in the same conversation.
   `flacli sync ... --yes` is only for when the user asked up front for everything confident to be fetched.
4. `flacli review <id> --doubtful` for the rest; approve or skip; queue again.
5. `flacli status <id>` later for the downloads, then `flacli m3u <id>`. Tell the user which tracks are missing.

## Rules

- Never queue downloads (`queue --yes`, `sync --yes`) without the user's explicit yes, except through `get`, where
  naming the music is the yes.
- Never delete files by hand. `flacli tidy` plans deletions; show every deletion by name; `--apply` only on a yes.
- If Nicotine+ is unreachable, say so and stop: the user must start it and enable the "MCP Bridge" plugin
  (Preferences > Plugins). Do not retry in a loop.
- Do not work around the Soulseek search rate limit; Soulseek bans the account for 30 minutes when it is exceeded.
  When status says the job is waiting on it, wait.
- Prefer FLAC. Pass `--lossy` only when the user wants lossy files.
- Do not paste whole tracklists into the conversation; use counts, ids and the slices `review` gives you.
- Never echo tokens, cookies or request headers.

## Reading the output

- `counts`: tracks by state. `pending` not matched yet, `candidates` need a decision, `approved` waiting to be
  queued, `queued` / `downloading` in flight, `done` on disk, `in_library` already owned, `not_found`, `failed`,
  `skipped`.
- `job.status`: `running`, `waiting` (rate limit), `finished`, `failed`, `cancelled`, `interrupted`. `job.phase` for
  a `get` job goes matching, queueing, finished. `job.worker_alive: false` means the background process died; run
  the command again.
- Candidate `confidence` at or above 0.85 is normally safe. Below that, read `why` (title, artist, album, duration
  agreement), `quality` and `queue` before approving.
