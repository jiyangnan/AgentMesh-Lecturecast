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
err()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

CONFLICTS=0
REGISTERED=0

backup_legacy_adapter() {
  target="$1"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="${target}.backup-${stamp}"
  suffix=1
  while [ -e "$backup" ] || [ -L "$backup" ]; do
    backup="${target}.backup-${stamp}-${suffix}"
    suffix=$((suffix + 1))
  done
  mv "$target" "$backup"
  printf '%s\n' "$backup"
}

manage_one() {
  agent="$1"
  base="$2"
  source="$3"
  target="$base/lecturecast"

  if [ ! -d "$base" ]; then
    host_root="${base%/skills}"
    if [ "$ACTION" = "install" ] && [ -d "$host_root" ]; then
      mkdir -p "$base"
      ok "$agent skills directory created"
    else
      warn "$agent adapter skipped: host not detected"
      return 0
    fi
  fi

  if [ "$ACTION" = "install" ]; then
    if [ -L "$target" ]; then
      current="$(readlink "$target")"
      if [ "$current" = "$source" ]; then
        ok "$agent adapter already registered"
        REGISTERED=$((REGISTERED + 1))
      else
        err "$agent adapter conflict: $target points to $current"
        err "rename that path, then rerun: bash \"$INSTALL_DIR/scripts/manage_adapters.sh\" install"
        CONFLICTS=$((CONFLICTS + 1))
      fi
      return 0
    fi
    if [ -e "$target" ]; then
      backup="$(backup_legacy_adapter "$target")"
      ok "$agent legacy adapter backed up to $backup"
      ln -s "$source" "$target"
      ok "$agent adapter upgraded"
      REGISTERED=$((REGISTERED + 1))
      return 0
    fi
    ln -s "$source" "$target"
    ok "$agent adapter registered"
    REGISTERED=$((REGISTERED + 1))
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
  manage_one "OpenClaw" "$HOME/.openclaw/skills" "$INSTALL_DIR/skills/openclaw"
else
  warn "OpenClaw adapter skipped: host not detected"
fi

if [ "$ACTION" = "install" ] && [ "$CONFLICTS" -gt 0 ]; then
  err "adapter registration blocked by $CONFLICTS conflict(s); LectureCast onboarding is not safe until resolved"
  exit 3
fi

if [ "$ACTION" = "install" ] && [ "$REGISTERED" -eq 0 ]; then
  warn "no supported agent host detected; install/register the agent Skill before using LectureCast"
fi
