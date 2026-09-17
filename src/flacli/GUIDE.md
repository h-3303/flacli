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
flacli avatar missing                 artists with no picture yet (the player shows it as the artist's avatar)
flacli avatar fill                    a picture per artist: artist folder, Wikidata portrait, MusicBrainz, Deezer
flacli avatar set "Artist" <file|url> use this picture instead (when fill found none, or the user prefers one)
flacli search "query"                 raw Soulseek search when the matcher found nothing
flacli wiki missing                   artists and albums with no bio / wiki text yet
flacli wiki fill                      Wikipedia text where an article exists; briefs with facts for the rest
flacli wiki sources "Artist" ["Album"]  MusicBrainz facts and links for one entry, to write from
flacli wiki set "Artist" ["Album"] --text-file t.txt --attribution "..."   store the text (pushed to the player)
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

## Workflow: bios and wikis for the library

The player (Flaclify / Euphonica) shows a bio under each artist and a wiki under each album; most are empty.
The text lives in a Markdown file beside the music (`Artist/artist.md`, `Artist/Album/wiki.md`) and is pushed
into the player's cache by `flacli wiki set`.

1. `flacli wiki missing` lists what has no text, artists first. Say how many.
2. `flacli wiki fill` gives every entry that has an English Wikipedia article its lead paragraph, attributed,
   ten entries per run (about four web requests each, one per second). Run it again while `remaining` > 0.
3. Everything in `to_write` is yours. Each entry comes with MusicBrainz facts (type, dates, area, labels, tags,
   annotation) and outbound links. Write from those facts and nothing else; if the facts are thin, say less.
   Use `flacli wiki sources "Artist" "Album"` to look one entry up on its own.
4. Store each text: `flacli wiki set "Artist" "Album" --text-file t.txt --attribution "Written by <you> from
   MusicBrainz and Discogs, 2026-09-17"`, or many at once with `--json list.json`. Existing text is kept unless
   `--force`.

How to write: plain prose, no markup, no headings. An album gets one paragraph (60 to 120 words): what it is,
when and where it came out, who made it, what it sounds like if a source says so. An artist gets two: who
they are and where from, then what they have made. Release level, not track by track. State facts; no
praise, no "iconic", no "seminal". The attribution is shown under the text: name the sources, and yourself
if you wrote it.

## Workflow: artist pictures (part of tidying)

The player shows a picture for each artist and finds almost none by itself. `flacli avatar` gives it one:
saved beside the music as `Artist/artist.jpg`, its origin in `.wiki/avatars.json`, pushed into the player's
cache so it shows at once.

1. `flacli avatar missing` lists artists without a picture. Say how many.
2. `flacli avatar fill` takes them ten at a time: a picture a tagger left in the artist folder, else the
   Wikidata portrait (Wikimedia Commons, author and licence recorded), else a MusicBrainz image relation,
   else Deezer's public artist picture (exact name match only). Run again while `remaining` > 0.
   `--providers wikidata,musicbrainz` leaves Deezer out when the user prefers free-licence pictures only.
3. `not_found` artists need a picture from the user: a file or a URL, then
   `flacli avatar set "Artist" <file|url> --attribution "..."`. Never invent a URL.
4. Report the `filled` list as artist, source and licence; name the ones still without a picture.

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
