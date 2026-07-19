#!/usr/bin/env bash
# Lecturecast installer (macOS / Linux). Idempotent.
set -euo pipefail

REPO="${LECTURECAST_REPO:-https://github.com/jiyangnan/AgentMesh-Lecturecast.git}"
BRANCH="${LECTURECAST_BRANCH:-main}"
INSTALL_DIR="${LECTURECAST_DIR:-$HOME/.lecturecast/app}"

case "$INSTALL_DIR" in
  ""|"/"|"$HOME")
    printf 'unsafe LECTURECAST_DIR: %s\n' "$INSTALL_DIR" >&2
    exit 1
    ;;
esac

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
PY_ARCH=$(python3 -c "import platform; print(platform.machine())")
HOST_ARCH=$(uname -m)
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 11 ]; }; then
  err "python 3.11+ required (found $PY_MAJ.$PY_MIN)"
  exit 1
fi
ok "python $PY_MAJ.$PY_MIN ($PY_ARCH)"
if [ "$(uname -s)" = "Darwin" ] && [ "$PY_ARCH" != "$HOST_ARCH" ]; then
  warn "mixed macOS architecture: host=$HOST_ARCH, python=$PY_ARCH"
  warn "if package wheels fail, install a native $HOST_ARCH Python 3.11+ and rerun"
fi

# --- fetch / update ---
if [ -d "$INSTALL_DIR/.git" ]; then
  ok "updating $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" --quiet
else
  if [ -e "$INSTALL_DIR" ]; then
    err "$INSTALL_DIR exists but is not a LectureCast git checkout; left unchanged"
    exit 1
  fi
  ok "cloning to $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

# --- venv + install ---
VENV="$INSTALL_DIR/.venv"
PY_SIGNATURE="$PY_MAJ.$PY_MIN/$PY_ARCH"
if [ -d "$VENV" ]; then
  VENV_SIGNATURE=""
  if [ -x "$VENV/bin/python" ]; then
    VENV_SIGNATURE=$("$VENV/bin/python" -c \
      'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}/{platform.machine()}")' \
      2>/dev/null || true)
  fi
  if [ "$VENV_SIGNATURE" != "$PY_SIGNATURE" ]; then
    warn "recreating incomplete or mismatched installer-owned venv ($VENV_SIGNATURE -> $PY_SIGNATURE)"
    rm -rf "$VENV"
  fi
fi
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  ok "venv created"
fi
if [ "${LECTURECAST_SKIP_PIP_UPGRADE:-0}" != "1" ]; then
  "$VENV/bin/pip" install --quiet --upgrade pip
fi
INSTALL_SPEC="$INSTALL_DIR"
if [ "${LECTURECAST_INSTALL_DIRECTOR:-0}" = "1" ]; then
  INSTALL_SPEC="$INSTALL_DIR[director]"
fi
if ! "$VENV/bin/pip" install --quiet -e "$INSTALL_SPEC"; then
  err "package installation failed; retrying with full diagnostics"
  "$VENV/bin/pip" install -e "$INSTALL_SPEC"
fi
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

# --- host-specific skills; never create host dirs or overwrite custom skills ---
bash "$INSTALL_DIR/scripts/manage_adapters.sh" install

# --- distinguish CLI installation from renderer readiness ---
DOCTOR_JSON=$("$VENV/bin/lecturecast" doctor --json)
RENDERER_READY=$(printf '%s' "$DOCTOR_JSON" | "$VENV/bin/python" -c \
  'import json, sys; print("1" if json.load(sys.stdin)["ready"] else "0")')
if [ "$RENDERER_READY" = "1" ]; then
  ok "CLI installed; renderer ready"
else
  warn "CLI installed; renderer not ready"
  "$VENV/bin/lecturecast" doctor
fi

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
bold "CLI installed. Next:"
echo "    lecturecast workflow      # where the local workflow lives"
echo "    lecturecast project resume <project-path> --json"
echo
echo "Community remains fully local. Director is optional; media and rendering stay local."
echo "Director signature verification is optional; install it only when needed:"
printf '    "%s/bin/pip" install '\''cryptography>=43'\''\n' "$VENV"
echo "If this agent session started before installation, open a new session and paste:"
echo "    请读取 LectureCast Skill，并从项目路径 <project-path> 继续。"
echo "Or generate an exact handoff payload: lecturecast director handoff <project-path> --json"
