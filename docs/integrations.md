# Using flacli from different agents

flacli has no harness-specific code. Pick the front door that fits the agent:

- **Shell.** Any agent that can run commands uses `flacli ...` directly. Give it `flacli guide` (or the contents of
  `src/flacli/GUIDE.md`) as instructions. This is the route for small local models: one command, one JSON result,
  a `next` field that says what to run.
- **Simple MCP.** `flacli mcp` over stdio: ten tools, one workflow step each. For MCP clients whose model is modest.
- **Full MCP.** `flacli mcp --full`: every fine-grained tool. For capable models that can plan a nine-step pipeline.
- **Raw Soulseek MCP.** `flacli mcp --soulseek`: only the Nicotine+ tools (search, browse, download, transfers).

`flacli` must be on the PATH of the process that launches the server (`install.sh` puts it in `~/.local/bin`).
Use the absolute path (`~/.local/bin/flacli`) where a client does not inherit your shell's PATH.

## Claude Code

Shell route: nothing to configure; add to the project's `CLAUDE.md`:

```
Music requests go through the `flacli` command. Run `flacli guide` once and follow it.
```

MCP route:

```bash
claude mcp add --scope user flacli -- flacli mcp          # or: flacli mcp --full
```

## Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.flacli]
command = "flacli"
args = ["mcp"]
```

Or the shell route with a line in `AGENTS.md` pointing at `flacli guide`.

## Gemini CLI

`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "flacli": { "command": "flacli", "args": ["mcp"] }
  }
}
```

## opencode

`opencode.json` (project) or `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "flacli": { "type": "local", "command": ["flacli", "mcp"], "enabled": true }
  }
}
```

## Goose

`~/.config/goose/config.yaml`:

```yaml
extensions:
  flacli:
    type: stdio
    cmd: flacli
    args: ["mcp"]
    enabled: true
```

## Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "flacli": { "command": "/home/you/.local/bin/flacli", "args": ["mcp"] }
  }
}
```

## Local models (Ollama, llama.cpp, LM Studio)

Two workable patterns:

1. **Shell tool.** Most local agent front-ends (Open Interpreter, aider's `/run`, smolagents, a bare tool-calling
   loop) can execute a command. Give the model `flacli guide --short` in its system prompt and a single tool that runs
   a shell command. A 7B model manages `flacli get "..."` followed by `flacli status <id>` reliably because there is
   nothing else to choose from.
2. **MCP client.** Any MCP-capable local client (mcphost, oterm, LM Studio's MCP support, Ollama front-ends with MCP)
   registers `flacli mcp`. Keep to the simple server; the full one has too many near-identical tools for a small
   model to pick between.

Whichever route: tell the model it must show the user the totals from `queue` before passing `--yes`, and that
`get` is the only command that downloads without that step.

## Environment and settings

Settings are read from `~/.config/flacli/config.toml` (`flacli config set key value`) and overridden by environment
variables, so an MCP server entry can carry per-client overrides:

```json
{ "command": "flacli", "args": ["mcp"], "env": { "FLACLI_MUSIC_DIR": "/srv/music", "FLACLI_CONTACT": "you@example.com" } }
```

| Setting | Env | Meaning |
| --- | --- | --- |
| `music_dir` | `FLACLI_MUSIC_DIR` | library scanned for tracks you already have (default `~/Music`) |
| `data_dir` | `FLACLI_DATA` | database, playlists, tokens (default `~/.local/share/flacli`) |
| `contact` | `FLACLI_CONTACT` | email or URL for the MusicBrainz User-Agent |
| `bridge_socket` | `NICOTINE_MCP_SOCKET` | only if you changed the socket in the Nicotine+ plugin settings |
| `tidal_client_id` | `FLACLI_TIDAL_CLIENT_ID` | your TIDAL app's client id |
| `auto_tidy` | `FLACLI_AUTO_TIDY` | file each finished download as Artist/Album/NN - Title (default on) |
| `wiki_targets` | `FLACLI_WIKI_TARGETS` | player caches to write bios, wikis and pictures into directly: `flaclify`, `euphonica`, or a path to a `metadata.sqlite`, comma-separated, empty for none. Flaclify also reads the files beside the music itself (default `flaclify`) |
| `mpd` | `FLACLI_MPD` | the player's MPD, told about new files and saved playlists: empty = auto (`$MPD_HOST`, the usual local sockets, `localhost:6600`), `off`, a socket path, or `[password@]host[:port]` |
