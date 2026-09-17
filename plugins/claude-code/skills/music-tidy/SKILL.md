---
name: music-tidy
description: Clean up and organise the local music library. Normalise tags, drop lossy duplicates of FLACs, remove playlist dumps, file everything as Artist/Album/NN - Title.ext, clear download reports out of the library, and give every artist a picture for the player. New downloads are filed automatically as they land (tidy_new does it for files that arrived any other way); this skill is the full pass for everything else. Use when the user says "/music-tidy", "tidy my music", "clean up the music folder", "sort the new downloads", "organise my library", "fill in the artist pictures", "the artist avatars are missing", or mentions stray tracks, duplicate m4a/flac copies, messy tags, or blank artist images.
tags: [music, tags, library]
---

# Music tidy

One library, one target state, one persistent decisions file. The `flacli` server's `tidy_analyse`
computes the whole plan; you read it, turn the open questions into approved decisions, show the user
the plan, and apply only on a yes. The user never has to restate the rules: they are below and in the
planner.

## Target state of the library

- `Artist/Album/NN - Title.ext`: album-artist folder, album folder, zero-padded track number. No track
  number, then `Title.ext`. Path characters sanitised (`/` to `-`, `:` to ` -`, `?*"<>` dropped).
- Every file has ARTIST, ALBUMARTIST, ALBUM, TITLE, TRACKNUMBER; DATE as `YYYY` or `YYYY-MM-DD` and
  identical across an album; track and disc numbers plain integers with totals in TOTAL* fields.
- ALBUMARTIST is the primary artist. Guests are `Primary feat. Guest` in ARTIST, never in ALBUMARTIST,
  never as `Title (feat. X)` when ARTIST already names them. FLAC multi-value ARTIST collapses to the
  primary artist.
- Multi-artist albums with no single primary become `Various Artists`. These are usually playlist
  dumps; ask whether to delete them, never assume.
- One copy per song: FLAC beats m4a/opus/mp3 of the same album-artist + title, even when the lossy copy
  is a music-video-length rip. Other duplicate edits ("(Official Video)", off-album copies) are listed
  and deleted only when approved.
- Nothing non-audio in the library root or artist folders except album extras (cover art, booklet PDF,
  `.nfo`) sitting inside their album folder. Download reports (`*.txt` starting "Download Report") go
  to `.tidy/reports/`. Backups and logs live in `.tidy/`.
- Dot-folders (`.thumbnails/` and the like) belong to other programs; the planner never enters them.

## New tracks file themselves

Every track `sync_downloads` marks done goes through a scoped pass at once (unless the setting
`auto_tidy` is off: `flacli config set auto_tidy off`): its tags are normalised by the same rules, it is moved to `Artist/Album/NN - Title`,
cover art and booklets follow once their download folder holds no more audio, and the emptied folder is
removed. Nothing else is touched and nothing is ever deleted: a file the full tidy would delete (a lossy
copy of a FLAC already owned), one missing ARTIST, ALBUM or TITLE, or one whose target path is taken is
left where it is and listed under `held` with the reason. `tidy_new(paths=None)` runs the same pass by
hand: with `paths` for specific files, without them after an incremental library scan (everything not
indexed before counts as new; files written in the last three minutes are left to settle and listed as
`settling`). When the user asks to "sort the new downloads", `tidy_new()` is the first thing to call;
what it holds, plus deletions and open questions, is what this skill's full procedure is for.

## Artist pictures

The player (Flaclify / Euphonica) shows a picture for each artist and finds almost none by itself; once
a lookup fails it never retries that artist. The four `avatar_*` tools give every artist one:

- `avatar_todo()`: artists with no picture beside their music (`Artist/artist.jpg`; an artist with no
  folder of their own gets `.wiki/<name>.jpg`). Say how many.
- `avatar_fill(limit=10)`: the first provider with a picture, in order: a picture a tagger left in the
  artist folder, the Wikidata portrait (Wikimedia Commons, author and licence recorded), a MusicBrainz
  image relation, Deezer's public artist picture (exact name match only). The picture is saved beside
  the music, its origin kept in `.wiki/avatars.json`, and pushed into the player's cache, replacing
  the player's failed-lookup memo, so it shows at once. Call again while `remaining` > 0.
  `providers="wikidata,musicbrainz"` leaves Deezer out when the user wants free-licence pictures only.
- `avatar_set(artist, source, attribution=None)`: one picture from a file or URL the user supplied,
  for the `not_found` ones or when they prefer another. Never invent a URL.
- `avatar_push(artist=None)`: copy the pictures beside the music into the player again (after its
  cache was cleared, or a picture was replaced by hand).

Report the filled artists as name, source and licence, and name those still without a picture. This is
step 6 of the procedure and is safe on its own: it writes only pictures, never tags or moves.

## Tools

- `tidy_new(paths=None, music_dir=None)`: the scoped pass above. Moves files but never deletes; safe
  without a plan review.
- `tidy_analyse(music_dir=None)`: dry run. Writes `<library>/.tidy/report.txt` and `plan.json`,
  returns a summary. Changes nothing.
- `tidy_apply(music_dir=None, confirm=False, force=False)`: without `confirm` it is another dry run.
  With `confirm=True` it backs up every tag, writes tags, deletes what the plan listed, moves files,
  files download reports and prunes empty folders, logging everything to `.tidy/tag_changes.log`.

`music_dir` defaults to the configured library (`flacli config`). Point it at a copy to rehearse. The
same planner is available from a shell as `flacli tidy [--apply] [--force] [root]`.

## Procedure

1. **Analyse.** Call `tidy_analyse`. If `recent_files` is non-empty a download is still landing; tell
   the user and wait, `tidy_apply` refuses anyway. The summary has the counts; read `report_path` whole
   for the detail rather than asking for slices.
2. **Resolve the open questions** by editing `approved_path` (`<library>/.tidy/approved.py`), which is
   persistent and cumulative. Comment every entry with the date and the reason. The report sections map
   to the tables:
   - Section 1, artist spelling variants: `ARTIST_CANON`. Default to the majority form; mention the
     official styling if it differs and let the user pick.
   - Section 3, missing ALBUM: `ALBUM_FILL`. Look the track up; for a one-off single use album = title.
     Uploader names mis-tagged as artist (`... - Uploader.opus`) go in `OVERRIDES`.
   - Sections 2 and 4, date disagreements: `DATE_PICK` (prefer the original release, not the reissue).
     Albums where no file carries a DATE at all: `DATE_FILL`, keyed `'albumartist|album'` (both through
     `akey` / `album_key` in the planner). Look them up on MusicBrainz: release-group first-release
     date, checked at release level when it looks like a promo; original release, not remaster or
     reissue; year only when the source gives just a month. Album title variants: `ALBUM_CANON` (strip
     "Artist - " prefixes, fix typos, title-case all-lowercase playlist names).
   - Section 4, `Various Artists` groups: ask whether they are playlist dumps to delete
     (`DELETE_ALBUMS`) or real compilations to keep.
   - Section 5b, duplicate edits: propose which copy to keep (album version, longer or lossless one)
     and put the loser in `DELETE`.
   - Section 7, strays: booklets, nfo and cover art go into their album folder via `MOVE`; anything
     else, ask. Failed downloads parsed from the reports are worth relaying; the user may want to
     retry them with playlist-sync.
3. **Re-analyse** until the summary has no open questions and no path clashes.
4. **Show the user the plan** in a few lines: counts of tag changes by rule, every deletion by name,
   the moves collapsed by album, the decisions you wrote into `approved.py`, and any failed downloads.
   Deletions are always listed individually. Then wait for a yes.
5. **Apply** with `tidy_apply(confirm=True)`, then call `tidy_analyse` once more and confirm it reports
   zero tag changes, zero deletions, zero moves. Report what was done in numbers, plus the backup path.
6. **Artist pictures.** `avatar_todo`, then `avatar_fill` until `remaining` is 0; ask the user for a
   file or URL for the rest and store it with `avatar_set`. Needs no approval: nothing is deleted or
   moved, and every picture's origin is recorded.

## Guardrails

- Never call `tidy_apply(confirm=True)` without step 4 in the same conversation.
- Never delete a file the plan did not list, and never delete by hand what the planner would handle.
- Do not pass `force=True` unless the user has said the recent files are not a download in progress.
- If `tidy_apply` reports errors, show them verbatim. The log and the backup in `.tidy/backups/` are
  the undo path.
- Do not dump the whole report into the conversation. Work with the summary counts and the sections
  that need a decision.

## Rules the planner applies by itself (no approval needed beyond the plan)

R1 trim whitespace in every text tag (lyrics left alone) · R2a strip ", Album" appended to ARTIST by
YouTube-style taggers · R2b normalise `feat.` · R3 set ALBUMARTIST · R4 unify album title spelling
within an album · R6 date format, propagated across album siblings · R6c fill DATE from `DATE_FILL` ·
R7 integer track and disc numbers · R10 FLAC beats lossy · R11 multi-value ARTIST to primary · R14 move
to `Artist/Album/NN - Title` · R15 download reports out · R16 prune empty folders.

Everything is logged tab-separated to `.tidy/tag_changes.log` with the rule that caused it, and a full
raw-tag snapshot is written to `.tidy/backups/` before any write.
