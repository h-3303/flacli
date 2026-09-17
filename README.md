# flacli

**Site:** https://flacli.vercel.app · **Repo:** https://github.com/h-3303/flacli

Get music onto disk from any agent, or from a shell: name songs and albums, or hand over a playlist; flacli
canonicalises the tracks on MusicBrainz, skips what your library already holds, fetches the rest on Soulseek
through your own running Nicotine+ client (one identity, your shares intact), files every track as
`Artist/Album/NN - Title` as it lands, and writes an M3U in the original order.

It is a fork of [claude-music](https://github.com/h-3303/claude-music) with the Claude Code plugin layer removed.
The same engine now has three front doors, so the tool works with whatever drives it:

| Front door | Run | For |
| --- | --- | --- |
| **CLI** | `flacli get "Lorde - Royals"` | any agent with a shell, including small local models; humans |
| **Simple MCP** | `flacli mcp` | MCP clients with a modest model: fifteen coarse tools, one step each |
| **Full MCP** | `flacli mcp --full` | capable models: every fine-grained tool (55) |

Every CLI command prints one JSON object and returns within seconds; Soulseek matching runs in a detached worker
and `flacli status` follows it. `flacli guide` prints the workflow for an agent to read.

```
agent ──shell──▶ flacli ─┬─▶ MusicBrainz (1 req/s, cached)
        or MCP           └─▶ Unix socket ──▶ MCP Bridge plugin ──▶ Nicotine+ core ──▶ Soulseek
```

No telemetry, no hosted services. The only network traffic is MusicBrainz lookups and Soulseek through Nicotine+.

## Install

Needs Nicotine+ 3.3 or newer and [uv](https://docs.astral.sh/uv/). Two halves: the `flacli` command, and a small
bridge plugin that runs inside Nicotine+.

```bash
git clone https://github.com/h-3303/flacli ~/src/flacli && cd ~/src/flacli
./install.sh
```

The installer copies `nicotine-plugin/mcp_bridge` into Nicotine+'s plugin folder (native or Flatpak) and runs
`uv tool install .` so `flacli` lands on your PATH. Then: Nicotine+ → Preferences → Plugins → enable plugins →
tick **MCP Bridge** (re-tick it after upgrading). Check with:

```bash
flacli doctor
flacli config set music_dir ~/Music
flacli config set contact you@example.com     # sent in the MusicBrainz User-Agent, as their terms ask
```

Settings live in `~/.config/flacli/config.toml`; environment variables (`FLACLI_MUSIC_DIR`, `FLACLI_DATA`,
`FLACLI_CONTACT`, `NICOTINE_MCP_SOCKET`, `FLACLI_TIDAL_CLIENT_ID`, `FLACLI_AUTO_TIDY`, `FLACLI_WIKI_TARGETS`,
`FLACLI_MPD`) override them.

## Use from a shell

```bash
flacli get "Lorde - Royals" "Boards of Canada - Geogaddi (album)"
flacli status 1                       # a minute later: job progress, downloads, where files went
flacli sync ~/Downloads/Playlist1.json
flacli status 2                       # until the job is finished
flacli queue 2                        # totals: tracks, MB, users
flacli queue 2 --yes                  # queue them
flacli review 2 --doubtful            # the matches below 0.85, with reasons
flacli approve 2 --tracks 14,15 && flacli skip 2 --tracks 16
flacli m3u 2
flacli tidy && flacli tidy --apply    # library clean-up: plan, then apply
flacli mpd                            # is the player's MPD reachable, and does it serve the same library?
flacli avatar fill                    # a picture for every artist: Wikidata portrait, MusicBrainz, Deezer
flacli cover fill                     # a cover for every album: Cover Art Archive, Deezer, iTunes; embedded too
flacli wiki missing                   # artists and albums whose bio / wiki in the player is empty
flacli wiki fill                      # Wikipedia's lead paragraph where an article exists, attributed
flacli wiki set "Artist" "Album" --text-file t.txt --attribution "Written by ... from MusicBrainz"
```

`flacli --help` and `flacli <command> --help` document every flag.

### The player

flacli files music; [Flaclify](https://github.com/h-3303/flaclify) (or any MPD client) plays it. The two meet
through MPD and through files beside the music, nothing else:

- **New files.** Each tidy ends with a scoped `update` of the folders that received files, so a finished download
  is in the player's library as soon as it is filed. A full `tidy --apply` updates the whole library once.
- **Playlists.** `flacli m3u` writes the M3U8 and also stores the playlist in MPD under its own name
  (`playlistclear` + `playlistadd`), so it appears in the player's Playlists view; tracks MPD does not know yet get
  their folders updated first. `flacli mpd playlist <id>` does the MPD half on its own.
- **Bios, wikis, pictures, covers.** `flacli wiki`, `flacli avatar` and `flacli cover` write `Artist/artist.md`,
  `Artist/Album/wiki.md`, `Artist/artist.jpg` and `Artist/Album/cover.jpg` beside the music. Flaclify reads the
  text and the artist pictures from those files itself, before any online provider, and re-reads one whenever it
  is newer than its cached copy; album art it takes only from its cache. The direct write into a player's cache
  (`wiki_targets`, default `flaclify`) is still there for Euphonica; set it to nothing once the files alone are
  enough. Covers are the exception: they go into Flaclify's cache whenever it exists, whatever `wiki_targets`
  says, because a failed lookup remembered there would otherwise hide the file for good.

MPD is found through `$MPD_HOST` / `$MPD_PORT`, then the usual local sockets, then `localhost:6600`;
`flacli config set mpd <socket path | host:port | off>` pins or disables it. Over a local socket flacli reads MPD's
`music_directory` and skips updates and playlists when it is not the library it files into; over TCP it assumes
they match. `flacli doctor` and `flacli mpd` report all of this. An unreachable MPD is a `skipped` field in the
result, never a failure.

### Artist pictures

The player shows a picture for each artist and finds almost none on its own. `flacli avatar fill` gives every
artist one: a picture a tagger left in the artist folder, else the Wikidata portrait (Wikimedia Commons, author
and licence recorded), else a MusicBrainz image relation, else Deezer's public artist picture for an exact name
match. It is saved beside the music as `Artist/artist.jpg`, where Navidrome and Jellyfin look too, its origin in
`.wiki/avatars.json`, and written into the player's image cache the way the player would have, so it shows at
once. An artist none of the sources has is remembered in the same file and not asked for again until
`--retry`, a new MusicBrainz id in the tags, or a picture dropped into the folder. `flacli avatar set "Artist"
photo.jpg` uses a picture of your own.

### Album covers

An album with no `cover.jpg` and no picture in its tracks shows as a grey square, and once the player's lookup
has failed it never asks again. `flacli cover fill` gives every album one: a picture a tagger left in the folder
or embedded in a track, else the Cover Art Archive front (the tagged release, then the release group MusicBrainz
finds), else Deezer's album search, else the iTunes Search API, the last two for an exact artist and title
match. It is saved as `Artist/Album/cover.jpg`, where MPD's `albumart`, Navidrome and Jellyfin look, embedded in
every track that has no picture (never replacing one; `--no-embed` leaves the tags alone), its origin kept in
`.wiki/covers.json`, and written into the player's cache under the album's folder as MPD names it, clearing the
failed-lookup memo. An album that already has its cover file only gets it embedded. An album none of the sources
has is remembered in the same file and not asked for again until `--retry`, a new MusicBrainz id in the tags, or
a picture dropped into the folder. `flacli cover set "Artist" "Album" front.png` uses a scan of your own.

### Bios and wikis

Flaclify and Euphonica show a bio under each artist and a wiki under each album, and for most libraries
they are empty. `flacli wiki` fills them: the text lives as `Artist/artist.md` and `Artist/Album/wiki.md`
beside the music (front matter with the source and licence, plain text below) and is pushed into the
player's `metadata.sqlite`, where it appears in the wiki panel and can be backed up to MPD from there.
`fill` takes Wikipedia verbatim where MusicBrainz links an article; for the rest, `sources` hands an agent
the MusicBrainz facts and links to write from, and `set` stores what it wrote with an attribution that
names the sources and the model. `wiki_targets` chooses the players (`flaclify`, `euphonica`, or a path).

## Use from an agent

Point the agent at the guide once (`flacli guide`, or paste `src/flacli/GUIDE.md` into its instructions) and give
it a shell. The guide is written for small models: a quick reference, two short workflows, and the rules that
matter (never queue downloads without the user's yes, never delete by hand, do not fight the Soulseek rate limit).

For MCP clients, register `flacli mcp` (simple) or `flacli mcp --full`. [docs/integrations.md](docs/integrations.md)
has the configuration for Claude Code, Codex CLI, Gemini CLI, opencode, Goose, Claude Desktop, and local
models through Ollama or llama.cpp.

### Claude Code plugin

`install.sh` also registers the repository as a plugin marketplace and installs the `flacli` plugin when
`claude` is on PATH; from inside Claude Code the equivalent is `/plugin marketplace add h-3303/flacli` then
`/plugin install flacli@flacli` (and `install.sh` for the bridge and the command). The plugin is a thin
wrapper: the two skills (`/playlist-sync`, `/music-tidy`), the matcher agent, a session health check and
a download monitor, over `flacli mcp --full` and `flacli mcp --soulseek` from PATH. It has no settings
of its own; `flacli config` is the one place. Coming from claude-music: `/plugin uninstall
claude-music@claude-music`, then the steps above; the Nicotine+ bridge plugin is the same and needs no
change.

## Services

- **TIDAL**: official API, browser login. Register your own app at developer.tidal.com with the redirect URI
  `http://127.0.0.1:43117/callback`, then `flacli config set tidal_client_id <id>` and `flacli service connect tidal`.
- **Deezer**: public playlists only, no login. `flacli service playlists deezer --user <id or profile URL>`.
- **YouTube Music**: unofficial (ytmusicapi). Copy the request headers from a logged-in music.youtube.com tab into a
  file and `flacli service connect youtube-music --headers-file headers.txt`.
- **Spotify**: the data export or an Exportify CSV (Spotify's developer terms forbid feeding API data to a model).

Tokens are stored 0600 under the flacli data dir and never leave the machine.

## Optional extras

The full MCP server also exposes [beets](https://beets.io) import and the ListenBrainz content resolver
(`troi`) when they are installed. See `flacli mcp --full`.

## Development

```bash
uv sync                      # dev environment
uv run pytest                # unit tests; bridge tests fetch Nicotine+ source into $XDG_CACHE_HOME/flacli
tests/run_matrix.sh          # against every supported Nicotine+ version
```

Tested against Nicotine+ 3.3.10, 3.3.11 and master with MCP Python SDK 2.x. GPL-3.0-or-later.
