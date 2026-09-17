# flacli — notes for Claude sessions

Music onto disk from any agent: named songs and albums or a playlist, canonicalised on MusicBrainz, diffed
against the local library, the rest fetched from Soulseek through the user's own running Nicotine+, filed as
`Artist/Album/NN - Title`, written as an M3U. Fork of `h-3303/claude-music` (2026-09-17) with the Claude Code
plugin layer taken out of the engine and put back as a thin wrapper. **This is the working project;
claude-music is frozen** (its README and site say so and link here).

Working rules, the user's: commit small with clear messages; never push without asking; no telemetry, no
hosted services, nothing uploaded anywhere; tests never touch the real network or the real Soulseek network;
setup instructions in one copy-pasteable block. Never submit this or claude-music to an Anthropic marketplace.

## Codemap

```
src/flacli/
  __init__.py       __version__ — bump it with pyproject.toml and the site's ld+json softwareVersion.
  config.py         Settings: SETTINGS maps key -> (env var, default, type). Precedence env > file >
                    default; the file is $FLACLI_CONFIG or ~/.config/flacli/config.toml (tomllib in,
                    a tiny writer out — strings and booleans only). data_dir()/music_dir()/db_path()/
                    playlists_dir()/logs_dir()/auth_dir()/bridge_socket_path()/user_agent()/auto_tidy().
                    bridge_socket_path() reads NICOTINE_MCP_SOCKET (the bridge's own name, kept),
                    then the file, then $XDG_RUNTIME_DIR/nicotine-mcp.sock (native) or the Flatpak
                    path. The Claude Code wrapper sets NO env: one source of truth, `flacli config`.
  cli.py            argparse; every command prints one JSON object (indent 1; --compact for one line),
                    errors print {"error", "command"} and exit 1. EXPECTED_ERRORS must include
                    ToolError: the tool functions in server.py are wrapped by tool_errors, which
                    converts domain errors to mcp ToolError. `flacli mcp [--full|--soulseek]` hands
                    off to the three servers. `flacli guide [--short]` prints GUIDE.md from the
                    package (importlib.resources; the file ships in the wheel because uv_build
                    includes package data).
  simple.py         The coarse operations behind the CLI and the simple MCP server: doctor, get, import,
                    prepare (resolve + scan + diff), sync, status, review, approve, skip, queue, wait_for,
                    cancel, m3u, delete, scan, tidy, tidy_new, service, search, downloads,
                    download_folder. Composes server.py's tool functions (they are plain async
                    functions; MCPServer.tool() returns the function unchanged). db() opens the
                    database and calls jobs.reap_stale() — NOT db.interrupt_running_jobs(), which
                    would kill a live worker's job row. close() at the end of every CLI run.
  jobs.py           Detached workers. spawn() writes the worker's pid into the job's progress JSON;
                    reap_stale() marks running jobs whose pid is gone (or, with no pid, untouched for
                    15 min) as interrupted; cancel() SIGTERMs a live worker or marks an orphan
                    cancelled; describe() is the job dict the status commands print (adds
                    worker_alive). Logs: <data_dir>/logs/job-<id>.log.
  worker.py         `python -m flacli.worker <match|request> <playlist_id> <job_id> <payload json>`.
                    Sets server.State.db / State.bridge, then runs MatchJob.run (match) or
                    server._request_job (request: match, auto-approve >= min_confidence, queue).
                    PREF_FIELDS is the serialisable subset of MatchPrefs; prefs_to()/prefs_from() are
                    the only way prefs cross the process boundary. SIGTERM/SIGINT cancel the task.
  server.py         The FULL MCP server (name "flacli", 30 tools) — the original claude-music library
                    server, tool names unchanged. State{db, bridge, jobs, mb_fetch, mb_sleep} is the
                    process-wide context that simple.py and worker.py also use; tests inject
                    mb_fetch/mb_sleep. In-process background jobs live in State.jobs (asyncio tasks)
                    when a job is started through this server; the CLI/simple server use workers
                    instead. Both write the same jobs table, so `flacli status` sees either.
  soulseek_mcp.py   The raw Nicotine+ MCP server (name "soulseek", 13 tools): search, browse, download,
                    transfers. Was a uv single-file script; now a module using config.bridge_socket_path().
  mcp_simple.py     The SIMPLE MCP server (name "flacli", 11 coarse tools) over simple.py, for small
                    models. `flacli mcp` runs this one by default.
  GUIDE.md          The agent guide (`flacli guide`). Written for the smallest model that might follow
                    it. Keep the "## Quick reference" heading: --short prints from it to the next "## ".
  bridge.py         Async client for the bridge socket (protocol v1/v2). BridgeUnavailable / RateLimited.
  db.py             SQLite (WAL, check_same_thread=False): playlists, tracks, matches, jobs, library
                    index, cache, user penalties. schema_version 1.
  matcher.py        MatchPrefs, MatchJob (searches through the bridge, scores results, album pass),
                    scoring functions. Honours the bridge's rate limit by waiting, never by retrying.
  requester.py      parse_items()/expand(): "Artist - Title", "Artist - Album (album)", dicts ->
                    tracks; albums expanded through MusicBrainz release lookups.
  musicbrainz.py    1 req/s client with cache table and User-Agent from config.user_agent().
  library.py        scan_library (mutagen index), diff_playlist, find_local, reindex_moved.
  tidy.py           The library planner: analyse() -> report + plan under <music>/.tidy, apply(),
                    apply_new() (scoped, never deletes). main() is a bare shell entry kept for tests.
  importers/        spotify_export, csvfile (Exportify + generic), m3ufile, xspf (JSPF/XSPF); FORMATS.
  connectors/       tidal (PKCE, loopback on 127.0.0.1:43117), deezer (public, no login), ytmusic
                    (ytmusicapi headers), oauth (TokenStore 0600 under auth_dir()).
  beets.py, troi_resolver.py   Optional extras exposed only by the full server.
  jspf.py, m3u.py, models.py, textnorm.py

plugins/claude-code/  The Claude Code wrapper plugin ("flacli"): .mcp.json runs `flacli mcp --full`
                    (server "flacli") and `flacli mcp --soulseek` (server "soulseek") from PATH; the
                    two skills (playlist-sync, music-tidy), the matcher agent, the SessionStart hook
                    (is `flacli` on PATH, is the bridge reachable — via `flacli --compact doctor`),
                    and the downloads monitor (polls the bridge, one line per finished/failed
                    transfer; reads data_dir and the socket from `flacli --compact config`). No
                    userConfig on purpose: settings come from `flacli config`, so the CLI an agent
                    runs in a shell and the MCP servers agree. .claude-plugin/marketplace.json at the
                    repo root registers it (`claude plugin marketplace add <repo>`; install.sh does it).
nicotine-plugin/mcp_bridge/  The Nicotine+ plugin, stdlib only, byte-identical to claude-music's.
                    Protocol v2; 0600 socket with SO_PEERCRED; search rate limit 34 per 220 s.
                    tests/harness.py loads it into a headless Nicotine+ core.
install.sh          Copies the bridge into Nicotine+'s plugin folder (native or Flatpak), `uv tool
                    install --force .` (flacli on ~/.local/bin), and, when `claude` is on PATH,
                    registers the repo as a marketplace and installs/updates the flacli plugin.
site/               The static site, flacli.vercel.app, DTTW style (dtw.css + fonts copied from the
                    claude-music site). og.png = headless-Chrome shot of index.html at 1200x630:
                    `google-chrome-stable --headless=new --window-size=1200,630 --screenshot=og.png file://.../index.html`.
                    Vercel project "flacli" (team hdig), git-connected, ROOT DIRECTORY = site; a push to
                    main deploys it. The .vercel link at the repo root is gitignored.
docs/integrations.md  Per-harness MCP/shell setup: Claude Code, Codex, Gemini CLI, opencode, Goose,
                    Claude Desktop, local models.
tests/              pytest; `uv run pytest`. conftest fetches Nicotine+ source into
                    $XDG_CACHE_HOME/flacli/nicotine-plus/<ref> (NICOTINE_PLUS_REF; run_matrix.sh for
                    3.3.10 + master) and runs a headless core in-process for the bridge tests.
                    test_cli.py (offline CLI + config), test_worker_e2e.py (get/sync through a real
                    detached worker against the harness, the simple server over stdio),
                    test_mcp_server.py (soulseek + full servers over stdio), test_e2e_pipeline.py
                    (the original pipeline), test_requests.py, test_tidy.py, test_connectors.py
                    (recorded fixtures), test_claude_plugin.py (manifests validate strictly when
                    `claude` is installed). 156+ tests, ~60 s.
```

## Cross-file couplings (each fails quietly)

- `worker.PREF_FIELDS` ↔ `matcher.MatchPrefs` fields. A new pref the CLI should pass must be added to
  PREF_FIELDS or it silently keeps its default in the worker.
- `simple._start_job()` creates the job row, then `jobs.spawn()` records the pid. A job created any
  other way has no pid and is reaped after 15 minutes of silence (STALE_AFTER_S) — the full server's
  in-process jobs included, if that server dies without marking them.
- `simple.db()` must never call `Database.interrupt_running_jobs()`; `server.db()` does, because the
  full MCP server owns its jobs in-process. If the CLI ever calls `server.db()` before `simple.db()`
  has set `State.db`, a live worker's job is marked interrupted. simple.py sets State.db first.
- Tool names in `server.py` ↔ the two skills and the matcher agent in `plugins/claude-code/`, which
  name them (bare names in the skills; fully prefixed `mcp__plugin_flacli_flacli__…` in the agent's
  `tools:` list). Renaming a tool or an MCP server name breaks them without an error.
- MCP server names: full server "flacli", raw server "soulseek", simple server "flacli". In Claude
  Code the plugin's servers are `flacli` and `soulseek` (prefix `mcp__plugin_flacli_<server>__`).
- `config.SETTINGS` ↔ `config.DESCRIPTIONS` ↔ docs/integrations.md's table ↔ README's settings line.
  `flacli config set` refuses unknown keys, so a new setting needs all four.
- `GUIDE.md` ↔ `cli.py` commands and flags. The guide is what an agent reads; a flag renamed in
  argparse and not in the guide is a flag the agent will get wrong.
- `mcp_simple.py` tool list ↔ `tests/test_worker_e2e.py::test_simple_mcp_server_tool_list` (exact set)
  ↔ the site's "eleven coarse tools" and README.
- `site/index.html` ld+json `softwareVersion` and the test count in the black band ↔ reality. Reshoot
  `og.png` when the top of the page changes; the portfolio plate
  (`~/dev/portfolio/assets/projects/flacli-site.webp`) is a separate shot, reshoot under a new name.
- `tests/libtools.FakeMusicBrainz` asserts the User-Agent starts with `flacli/`.
- The bridge plugin is shared with claude-music. A protocol change here must stay compatible with
  the claude-music servers still installed on other machines, or bump the protocol and handle both.

## Failure modes (happened)

1. **Vercel git connection + root directory.** `vercel link` auto-connected the GitHub repo. The
   first push to main built the repo root (no index.html) and the 404 deployment took the alias.
   Fix: `rootDirectory: "site"` on the project (set through the API with the CLI's token from
   `~/.local/share/com.vercel.cli/auth.json`; the CLI has no flag for it), re-link from the repo root.
2. **CLI called a tool that raised ToolError and crashed with a traceback** instead of printing
   `{"error"}`. server.py's tool_errors converts domain errors to `mcp...ToolError`; the CLI must
   catch that too.
3. **`sync` reached MusicBrainz before checking the bridge**, so an offline test hit the live API and a
   user without Nicotine+ running would wait minutes before the failure. Bridge check first.
4. **`.repo-nav { display:flex }` beat `[hidden]`** (portfolio) — author display rules override the
   UA's `[hidden]`; add an explicit `[hidden] { display: none }`.
5. **Heredocs and apostrophes.** A `bash -c '…'` wrapping a script that contains `'` ends the string
   mid-script; one such failure still ran the `git commit` half with a truncated message. Write
   scripts and commit messages to the scratchpad and run/`-F` them.

## Verifying

```sh
uv run pytest -q                       # everything, ~60 s; needs a Nicotine+ source checkout (cached)
uv run pytest tests/test_cli.py -q     # offline only
uv run flacli doctor                   # against this machine's Nicotine+
tests/run_matrix.sh                    # 3.3.10 and master
```

For the Claude Code wrapper: `claude plugin validate plugins/claude-code --strict` and
`claude plugin validate . --strict`. After changing it locally: `claude plugin marketplace update flacli`
then `claude plugin update flacli@flacli` (a directory marketplace reads the working tree, but the
installed copy is a cache).

## Releasing

1. Bump `__version__`, pyproject, the site's ld+json, the plugin.json and marketplace.json versions.
2. `uv run pytest`. 3. Commit; ask before pushing. A push deploys the site; the plugin is installed
from the repo by marketplace, so users update with `/plugin marketplace update flacli` +
`/plugin update flacli@flacli`. 4. `./install.sh` here to refresh the local `uv tool` install.
