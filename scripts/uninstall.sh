#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${LECTURECAST_DIR:-$HOME/.lecturecast/app}"
SHIM="$HOME/.local/bin/lecturecast"

bash "$INSTALL_DIR/scripts/manage_adapters.sh" uninstall

if [ -f "$SHIM" ] && grep -Fq "$INSTALL_DIR/.venv/bin/lecturecast" "$SHIM"; then
  rm "$SHIM"
  printf '  \033[32m✓\033[0m LectureCast shim removed\n'
elif [ -e "$SHIM" ]; then
  printf '  \033[33m⚠\033[0m custom lecturecast shim left unchanged\n'
fi

printf 'LectureCast adapters are unregistered.\n'
printf 'The app checkout and all local projects were preserved: %s\n' "$INSTALL_DIR"
printf 'Set LECTURECAST_REMOVE_APP=1 and remove that checkout manually only after backing up custom files.\n'
