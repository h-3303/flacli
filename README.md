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
| **Simple MCP** | `flacli mcp` | MCP clients with a modest model: ten coarse tools, one step each |
| **Full MCP** | `flacli mcp --full` | capable models: every fine-grained tool (43) |

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
`FLACLI_CONTACT`, `NICOTINE_MCP_SOCKET`, `FLACLI_TIDAL_CLIENT_ID`, `FLACLI_AUTO_TIDY`) override them.

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
```

`flacli --help` and `flacli <command> --help` document every flag.

## Use from an agent

Point the agent at the guide once (`flacli guide`, or paste `src/flacli/GUIDE.md` into its instructions) and give
it a shell. The guide is written for small models: a quick reference, two short workflows, and the rules that
matter (never queue downloads without the user's yes, never delete by hand, do not fight the Soulseek rate limit).

For MCP clients, register `flacli mcp` (simple) or `flacli mcp --full`. [docs/integrations.md](docs/integrations.md)
has the configuration for Claude Code, Codex CLI, Gemini CLI, opencode, Goose, Claude Desktop, and local
models through Ollama or llama.cpp.

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
