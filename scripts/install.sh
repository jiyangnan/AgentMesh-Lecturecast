#!/usr/bin/env bash
# Lecturecast installer (macOS / Linux). Idempotent.
set -euo pipefail

REPO="${LECTURECAST_REPO:-https://github.com/jiyangnan/AgentMesh-Lecturecast.git}"
BRANCH="${LECTURECAST_BRANCH:-main}"
INSTALL_DIR="${LECTURECAST_DIR:-$HOME/.lecturecast/app}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m⚠\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }

bold "Lecturecast installer"

# --- prereqs ---
need_cmd() { command -v "$1" >/dev/null 2>&1 || { err "missing: $1"; exit 1; }; }
need_cmd python3
need_cmd git

PY_MAJ=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MIN=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 11 ]; }; then
  err "python 3.11+ required (found $PY_MAJ.$PY_MIN)"
  exit 1
fi
ok "python $PY_MAJ.$PY_MIN"

# --- fetch / update ---
if [ -d "$INSTALL_DIR/.git" ]; then
  ok "updating $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" --quiet
else
  ok "cloning to $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

# --- venv + install ---
VENV="$INSTALL_DIR/.venv"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  ok "venv created"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$INSTALL_DIR"
ok "lecturecast package installed"

# --- shim on PATH ---
SHIM_DIR="$HOME/.local/bin"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/lecturecast" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/lecturecast" "\$@"
EOF
chmod +x "$SHIM_DIR/lecturecast"
ok "shim at $SHIM_DIR/lecturecast"

# --- PATH hint ---
case ":$PATH:" in
  *":$SHIM_DIR:"*) ok "$SHIM_DIR is on PATH" ;;
  *)
    warn "add $SHIM_DIR to PATH; one of:"
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && exec zsh"
    echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && exec bash"
    ;;
esac

echo
bold "Installed. Next:"
echo "    lecturecast init --key lc_live_xxxxxxxx"
echo "    lecturecast new \"RAG 工作原理\""
echo
echo "Get a free M1 license: https://lecturecast.agentmesh360.com"
