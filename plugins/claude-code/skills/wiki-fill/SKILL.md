---
name: wiki-fill
description: Fill the empty artist bios and album wikis the music player (Flaclify / Euphonica) shows. Wikipedia's lead paragraph where an article exists, your own prose from MusicBrainz facts for the rest, every text attributed and kept as Markdown beside the music. Use when the user says "/wiki-fill", "fill in the wikis", "write the bios", "the album wiki is empty", "add artist info to the player", or after new albums have landed and they want them described.
tags: [music, metadata, library]
---

# Wiki fill

The `flacli` server's four `wiki_*` tools do the bookkeeping; you do the writing. Text goes to
`Artist/artist.md` and `Artist/Album/wiki.md` in the music folder and, from there, into the player's
metadata cache, where it shows up in the wiki panel the next time the page is opened. Nothing is sent
anywhere.

## Steps

1. `wiki_todo()` lists every artist and album without text, artists first. Tell the user the counts.
2. `wiki_fill(limit=10)` gives each entry that has an English Wikipedia article (via MusicBrainz and
   Wikidata) its lead paragraph, attributed CC BY-SA. Repeat while `remaining` is above zero. One
   entry costs about four web requests at one per second; say so if the list is long.
3. Every entry in `to_write` is yours. It carries MusicBrainz facts (type, dates, area, labels, tags,
   annotation, artist credit) and outbound links. Write from those facts only. `wiki_sources` looks one
   entry up again on its own.
4. `wiki_write(artist, content, attribution, album=None, url=None)` stores each text. Do not pass
   `force` unless the user asked to replace what is there.

## How to write

- Plain prose, no markup, no headings, no lists. The player shows it as plain text.
- An album: one paragraph, 60 to 120 words. What it is, when and where it came out, who made it, what
  it sounds like when a source says so. Release level, not track by track.
- An artist: two paragraphs. Who they are and where from; then what they have made, by release.
- State facts. No praise, no "iconic", "seminal", "legendary". If the facts are thin, write less; never
  invent a date, a place or a lineup.
- Attribution names the sources and you, with the date: `Written by Claude from MusicBrainz and
  Discogs, 2026-09-17`. It is shown under the text.
- When the user's library holds music you know well from training but the sources say little, you may
  add what you are sure of; if in doubt, leave it out.

## Guardrails

- Never write into the player's database by any other route; `wiki_write` is the only writer.
- Existing text is kept. Replacing it needs the user's yes and `force=True`.
- Do not paste whole texts back into the conversation unless asked; report counts and titles.
