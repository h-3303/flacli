# SPDX-License-Identifier: GPL-3.0-or-later
"""Paths and settings, all local.

Precedence: environment variable > config file > default. The file is TOML at $FLACLI_CONFIG, else
$XDG_CONFIG_HOME/flacli/config.toml (~/.config/flacli/config.toml). `flacli config` reads and writes it.
"""

import os
import tomllib

from pathlib import Path

from . import __version__

# key -> (environment variable, default, type)
SETTINGS = {
    "data_dir": ("FLACLI_DATA", "", str),
    "music_dir": ("FLACLI_MUSIC_DIR", "~/Music", str),
    "contact": ("FLACLI_CONTACT", "", str),
    "bridge_socket": ("NICOTINE_MCP_SOCKET", "", str),
    "tidal_client_id": ("FLACLI_TIDAL_CLIENT_ID", "", str),
    "tidal_redirect_uri": ("FLACLI_TIDAL_REDIRECT_URI", "http://127.0.0.1:43117/callback", str),
    "auto_tidy": ("FLACLI_AUTO_TIDY", True, bool),
    "wiki_targets": ("FLACLI_WIKI_TARGETS", "flaclify", str),
    "mpd": ("FLACLI_MPD", "", str),
}

DESCRIPTIONS = {
    "data_dir": "Where flacli keeps its database, playlists and tokens (default: ~/.local/share/flacli)",
    "music_dir": "The music library scanned for tracks you already have",
    "contact": "Email or URL sent in the User-Agent of MusicBrainz lookups, as their API terms ask",
    "bridge_socket": "Nicotine+ MCP Bridge socket, only if you changed it in the Nicotine+ plugin settings",
    "tidal_client_id": "Client id of your own app at developer.tidal.com (redirect URI http://127.0.0.1:43117/callback)",
    "tidal_redirect_uri": "Loopback redirect registered on the TIDAL app",
    "auto_tidy": "File each finished download as Artist/Album/NN - Title with normalised tags (never deletes)",
    "wiki_targets": "Player caches to write bios, wikis and pictures into directly: flaclify, euphonica, or a path to a "
                    "metadata.sqlite, comma-separated; empty for none. Flaclify also reads the files beside the music itself "
                    "(default: flaclify)",
    "mpd": "The player's MPD, told about new files and saved playlists: empty = auto ($MPD_HOST, the usual local sockets, "
           "localhost:6600), off, a socket path, or [password@]host[:port]",
}


def config_path() -> Path:
    configured = os.environ.get("FLACLI_CONFIG")

    if configured:
        return Path(configured).expanduser()

    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "flacli" / "config.toml"


def read_file() -> dict:
    path = config_path()

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from None

    return {k: v for k, v in data.items() if k in SETTINGS}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value

    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def get(key: str):
    """The effective value of one setting."""
    env_name, default, kind = SETTINGS[key]
    raw = os.environ.get(env_name)

    if raw is None or raw == "":
        raw = read_file().get(key, default)

    if kind is bool:
        return _as_bool(raw)

    return str(raw).strip() if raw is not None else ""


def effective() -> dict:
    """Every setting with its value and where it came from (env / file / default)."""
    file_values = read_file()
    result = {}

    for key, (env_name, default, kind) in SETTINGS.items():
        if os.environ.get(env_name):
            source = f"env {env_name}"
        elif key in file_values:
            source = "file"
        else:
            source = "default"

        result[key] = {"value": get(key), "source": source, "description": DESCRIPTIONS[key]}

    result["data_dir"]["value"] = str(data_dir())
    result["music_dir"]["value"] = str(music_dir())
    result["bridge_socket"]["value"] = bridge_socket_path()
    return result


def write_setting(key: str, value) -> Path:
    """Set one key in the config file (created when missing). Values are plain strings or booleans."""
    if key not in SETTINGS:
        raise ValueError(f"unknown setting {key!r}; known: {', '.join(SETTINGS)}")

    kind = SETTINGS[key][2]
    current = read_file()
    current[key] = _as_bool(value) if kind is bool else str(value)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# flacli settings. Environment variables override these (see `flacli config`)."]

    for name, item in current.items():
        if isinstance(item, bool):
            lines.append(f"{name} = {'true' if item else 'false'}")
        else:
            escaped = str(item).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{name} = "{escaped}"')

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def data_dir() -> Path:
    configured = get("data_dir")

    if configured:
        return Path(configured).expanduser()

    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "flacli"


def music_dir() -> Path:
    return Path(get("music_dir") or "~/Music").expanduser()


def db_path() -> Path:
    return data_dir() / "state.db"


def playlists_dir() -> Path:
    return data_dir() / "playlists"


def logs_dir() -> Path:
    return data_dir() / "logs"


def user_agent() -> str:
    contact = get("contact") or "contact-not-configured"
    return f"flacli/{__version__} ( {contact} )"


def bridge_socket_path() -> str:
    configured = get("bridge_socket")

    if configured:
        return configured

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
    candidates = [
        os.path.join(runtime_dir, "nicotine-mcp.sock"),                                        # native package
        os.path.join(runtime_dir, "app", "org.nicotine_plus.Nicotine", "nicotine-mcp.sock"),  # Flatpak
    ]
    return next((c for c in candidates if os.path.exists(c)), candidates[0])


def auth_dir():
    """0700 folder for service tokens (never in the repo, never in the config file)."""
    path = data_dir() / "auth"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def tidal_client_id() -> str:
    return get("tidal_client_id")


def tidal_redirect_uri() -> str:
    """Loopback redirect for the TIDAL PKCE flow; must be registered verbatim on the TIDAL app."""
    return get("tidal_redirect_uri") or "http://127.0.0.1:43117/callback"


def auto_tidy() -> bool:
    """Tidy newly downloaded tracks (tags + Artist/Album/NN - Title) as soon as a sync sees them finish."""
    return get("auto_tidy")


def wiki_targets() -> str:
    """Player caches `flacli wiki` and `flacli avatar` also write into directly (flaclify, euphonica, or a
    metadata.sqlite path), comma-separated; empty for none. Flaclify reads the sidecar files itself as well."""
    return get("wiki_targets")


def mpd_address() -> str:
    """The `mpd` setting as written: "" auto, "off", a socket path, or [password@]host[:port]."""
    return get("mpd")
