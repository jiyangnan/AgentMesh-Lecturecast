#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-install}"
INSTALL_DIR="${LECTURECAST_DIR:-$HOME/.lecturecast/app}"

if [ "$ACTION" != "install" ] && [ "$ACTION" != "uninstall" ]; then
  printf 'usage: %s [install|uninstall]\n' "$0" >&2
  exit 2
fi

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }

manage_one() {
  agent="$1"
  base="$2"
  source="$3"
  target="$base/lecturecast"

  # An absent host directory means that agent is not installed/configured.
  # Do not create it merely because LectureCast is being installed.
  if [ ! -d "$base" ]; then
    host_root="${base%/skills}"
    if [ -d "$host_root" ]; then
      warn "$agent adapter skipped: $base is missing; create it and rerun this installer"
    else
      warn "$agent adapter skipped: host not detected"
    fi
    return 0
  fi

  if [ "$ACTION" = "install" ]; then
    if [ -L "$target" ]; then
      current="$(readlink "$target")"
      if [ "$current" = "$source" ]; then
        ok "$agent adapter already registered"
      else
        warn "$agent already has a custom lecturecast symlink; left unchanged"
      fi
      return 0
    fi
    if [ -e "$target" ]; then
      warn "$agent already has a custom lecturecast skill; left unchanged"
      return 0
    fi
    ln -s "$source" "$target"
    ok "$agent adapter registered"
    return 0
  fi

  if [ -L "$target" ] && [ "$(readlink "$target")" = "$source" ]; then
    rm "$target"
    ok "$agent adapter unregistered"
  elif [ -e "$target" ] || [ -L "$target" ]; then
    warn "$agent lecturecast skill is not installer-owned; left unchanged"
  fi
}

manage_one "Codex" "$HOME/.codex/skills" "$INSTALL_DIR/skills/codex"
manage_one "Claude Code" "$HOME/.claude/skills" "$INSTALL_DIR/skills/claude-code"

if [ -d "$HOME/.openclaw/skills" ]; then
  manage_one "OpenClaw" "$HOME/.openclaw/skills" "$INSTALL_DIR/skills/openclaw"
elif [ -d "$HOME/.openclaw/workspace/skills" ]; then
  manage_one "OpenClaw" "$HOME/.openclaw/workspace/skills" "$INSTALL_DIR/skills/openclaw"
elif [ -d "$HOME/.openclaw" ]; then
  warn "OpenClaw adapter skipped: no skills directory detected; create one and rerun this installer"
else
  warn "OpenClaw adapter skipped: host not detected"
fi
