#!/usr/bin/env bash
# Installs the Nicotine+ MCP bridge plugin, the flacli command, and (when claude is on PATH) the Claude Code plugin.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(pwd)

if [[ -d ~/.var/app/org.nicotine_plus.Nicotine ]] && ! command -v nicotine >/dev/null; then
  PLUGIN_DIR=~/.var/app/org.nicotine_plus.Nicotine/data/nicotine/plugins   # Flatpak
else
  PLUGIN_DIR=${XDG_DATA_HOME:-~/.local/share}/nicotine/plugins
fi

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }

# 1. Nicotine+ plugin
mkdir -p "$PLUGIN_DIR"
rm -rf "$PLUGIN_DIR/mcp_bridge"
cp -r nicotine-plugin/mcp_bridge "$PLUGIN_DIR/"
echo "Nicotine+ plugin installed to $PLUGIN_DIR/mcp_bridge"

# 2. The flacli command (its own environment under uv's tool dir; ~/.local/bin/flacli on PATH)
uv tool install --force --quiet "$REPO"
echo "flacli installed: $(command -v flacli || echo "$HOME/.local/bin/flacli (add ~/.local/bin to PATH)")"

# 3. Claude Code plugin (skills, matcher agent, health check, download monitor over the flacli command).
#    A fresh checkout is registered as a local marketplace; a marketplace added from GitHub
#    (/plugin marketplace add h-3303/flacli) is refreshed instead.
if command -v claude >/dev/null; then
  if ! claude plugin marketplace add "$REPO" >/dev/null 2>&1; then
    claude plugin marketplace update flacli >/dev/null 2>&1 || true
  fi

  if claude plugin list 2>/dev/null | grep -q 'flacli@flacli'; then
    claude plugin update flacli@flacli >/dev/null
  else
    claude plugin install flacli@flacli >/dev/null
  fi
  echo "Claude Code plugin 'flacli' installed (from $REPO)"
fi

echo
echo "Now: Nicotine+ → Preferences → Plugins → enable plugins → tick 'MCP Bridge'."
echo "Then: flacli doctor    (and flacli config set music_dir ~/Music if your library is elsewhere)"
echo "Agents: flacli guide   |   MCP clients: flacli mcp   (see docs/integrations.md)"
