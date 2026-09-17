"""Locate (and if necessary clone) the Nicotine+ source tree the tests run against.

Selection, in order:
  NICOTINE_PLUS_SRC   explicit path to a Nicotine+ checkout (must contain pynicotine/)
  NICOTINE_PLUS_REF   git ref to use from the cache (default "3.3.10"; "master" is the dev branch)

Checkouts are cached under $XDG_CACHE_HOME/flacli/nicotine-plus/<ref>. Cloning needs the
network once; set NICOTINE_PLUS_OFFLINE=1 to forbid cloning (tests then skip if the ref is absent).
NICOTINE_PLUS_UPDATE=1 fetches the latest commit for branch refs such as "master".
"""

import os
import subprocess

from pathlib import Path

DEFAULT_REF = "3.3.10"
REPO_URL = "https://github.com/nicotine-plus/nicotine-plus.git"


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "flacli" / "nicotine-plus"


def selected_ref() -> str:
    return os.environ.get("NICOTINE_PLUS_REF", DEFAULT_REF)


def _run_git(args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=300
    ).stdout.strip()


def find_source(ref: str | None = None) -> tuple[Path | None, str]:
    """Return (path, reason). path is None when no usable checkout could be obtained."""

    explicit = os.environ.get("NICOTINE_PLUS_SRC")

    if explicit:
        path = Path(explicit).expanduser().resolve()

        if (path / "pynicotine" / "__init__.py").is_file():
            return path, f"NICOTINE_PLUS_SRC={path}"

        return None, f"NICOTINE_PLUS_SRC={path} does not contain pynicotine/"

    ref = ref or selected_ref()
    path = cache_root() / ref
    offline = os.environ.get("NICOTINE_PLUS_OFFLINE") == "1"

    if (path / "pynicotine" / "__init__.py").is_file():
        if os.environ.get("NICOTINE_PLUS_UPDATE") == "1" and not offline:
            try:
                _run_git(["pull", "--ff-only", "--quiet"], cwd=path)
            except subprocess.CalledProcessError as error:
                return path, f"cached checkout {path} (update failed: {error.stderr.strip()})"

        return path, f"cached checkout {path}"

    if offline:
        return None, f"no cached Nicotine+ checkout at {path} and NICOTINE_PLUS_OFFLINE=1"

    path.parent.mkdir(parents=True, exist_ok=True)
    branch_args = [] if ref == "master" else ["--branch", ref]

    try:
        _run_git(["clone", "--quiet", "--depth", "1", *branch_args, REPO_URL, str(path)])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        return None, f"could not clone Nicotine+ {ref} (offline?): {detail.strip()}"

    return path, f"fresh clone {path}"


def describe(path: Path) -> str:
    try:
        return _run_git(["log", "-1", "--format=%h %cs"], cwd=path)
    except Exception:  # pragma: no cover - cosmetic
        return "unknown commit"
