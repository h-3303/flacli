"""The Claude Code wrapper plugin: manifests validate strictly, the MCP config runs flacli, the agent names
tools the full server has."""

import json
import re
import shutil
import subprocess

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "claude-code"

needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")


@needs_claude
@pytest.mark.parametrize("target", [PLUGIN, REPO])
def test_manifests_validate_strictly(target):
    proc = subprocess.run(["claude", "plugin", "validate", str(target), "--strict"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_marketplace_points_at_plugin():
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = marketplace["plugins"]
    assert entry["name"] == "flacli"
    assert (REPO / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == entry["version"]


def test_mcp_config_runs_flacli():
    config = json.loads((PLUGIN / ".mcp.json").read_text())
    servers = config["mcpServers"]
    assert set(servers) == {"flacli", "soulseek"}
    assert servers["flacli"] == {"command": "flacli", "args": ["mcp", "--full"]}
    assert servers["soulseek"] == {"command": "flacli", "args": ["mcp", "--soulseek"]}


def test_agent_tools_exist_on_the_full_server():
    from flacli import server

    text = (PLUGIN / "agents" / "matcher.md").read_text()
    names = re.findall(r"mcp__plugin_flacli_flacli__(\w+)", text)
    assert names
    for name in names:
        assert callable(getattr(server, name, None)), name


def test_skills_name_the_new_servers():
    for skill in ("playlist-sync", "music-tidy"):
        text = (PLUGIN / "skills" / skill / "SKILL.md").read_text()
        assert "`library`" not in text and "`nicotine`" not in text and "claude-music" not in text
