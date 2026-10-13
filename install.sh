#!/usr/bin/env bash
# Installs or updates this fork of cptr (branch deploy) and starts it in the background with nohup.
# Running it again updates: it pulls, rebuilds the frontend and restarts the server.
#
#   curl -fsSL https://raw.githubusercontent.com/ciberick/computer/deploy/install.sh | bash
#   CPTR_PORT=9741 bash install.sh
#
# Settings (environment variables):
#   CPTR_DIR      where the repo lives            (default: ~/computer)
#   CPTR_BRANCH   branch to install               (default: deploy)
#   CPTR_HOST     address to bind                 (default: 0.0.0.0)
#   CPTR_PORT     port to bind                    (default: 8000)
#   CPTR_EXTRAS   optional extras, comma-separated (default: agents)
# CPTR_DATA_DIR, if set, is passed through to cptr as usual.
set -euo pipefail

REPO_URL="${CPTR_REPO_URL:-https://github.com/ciberick/computer.git}"
BRANCH="${CPTR_BRANCH:-deploy}"
DIR="${CPTR_DIR:-$HOME/computer}"
HOST="${CPTR_HOST:-0.0.0.0}"
PORT="${CPTR_PORT:-8000}"
EXTRAS="${CPTR_EXTRAS:-agents}"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- Requirements -----------------------------------------------------------------------------

command -v git >/dev/null || die "git is not installed"
command -v npm >/dev/null || die "Node.js/npm is not installed (the frontend build needs Node 20.19+ or 22.12+)"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 20 ] || die "Node $(node -v) is too old: the frontend build needs Node 20.19+ or 22.12+"

if ! command -v uv >/dev/null; then
	if [ -x "$HOME/.local/bin/uv" ]; then
		export PATH="$HOME/.local/bin:$PATH"
	else
		log "Installing uv"
		curl -LsSf https://astral.sh/uv/install.sh | sh
		export PATH="$HOME/.local/bin:$PATH"
	fi
fi

# --- Code -------------------------------------------------------------------------------------

if [ -d "$DIR/.git" ]; then
	log "Updating $DIR ($BRANCH)"
	# Local edits would be silently mixed into the build, or block the pull halfway.
	[ -z "$(git -C "$DIR" status --porcelain --untracked-files=no)" ] ||
		die "$DIR has local changes; commit or stash them first (git -C $DIR status)"
	git -C "$DIR" fetch origin "$BRANCH"
	git -C "$DIR" checkout -q "$BRANCH"
	git -C "$DIR" merge --ff-only "origin/$BRANCH"
else
	log "Cloning $REPO_URL ($BRANCH) into $DIR"
	git clone -b "$BRANCH" "$REPO_URL" "$DIR"
fi
cd "$DIR"

# --- Build ------------------------------------------------------------------------------------

log "Installing Python dependencies (extras: $EXTRAS)"
extra_args=()
IFS=',' read -ra extras <<<"$EXTRAS"
for extra in "${extras[@]}"; do
	if [ -n "$extra" ]; then extra_args+=(--extra "$extra"); fi
done
# --locked: install exactly what uv.lock pins, and never rewrite it (that would block the next update).
uv sync --locked "${extra_args[@]}"

log "Building the frontend"
(cd cptr/frontend && npm ci && npm run build)

# --- Restart ----------------------------------------------------------------------------------

PID_FILE="$DIR/cptr.pid"
LOG_FILE="$DIR/cptr.log"

# The server this script started last time, or one started by hand with nohup from this same
# directory (the way it was done before this script). Other cptr instances are left alone.
old_pids=()
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
	old_pids+=("$(cat "$PID_FILE")")
elif [ -d /proc ]; then
	for pid in $(pgrep -u "$(id -u)" -f "cptr run" || true); do
		[ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$DIR" ] && old_pids+=("$pid")
	done
fi

if [ "${#old_pids[@]}" -gt 0 ]; then
	log "Stopping the running server (pid ${old_pids[*]})"
	kill "${old_pids[@]}" 2>/dev/null || true
	for _ in $(seq 20); do
		kill -0 "${old_pids[@]}" 2>/dev/null || break
		sleep 0.5
	done
	kill -9 "${old_pids[@]}" 2>/dev/null || true
fi

log "Starting cptr on $HOST:$PORT (log: $LOG_FILE)"
# The venv's own entry point, not `uv run`, so the pid saved is the server itself.
nohup "$DIR/.venv/bin/cptr" run --host "$HOST" --port "$PORT" --headless >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

for _ in $(seq 60); do
	if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
		version="$("$DIR/.venv/bin/python" -c 'from importlib.metadata import version; print(version("cptr"))')"
		log "cptr $version is up on port $PORT ($(git rev-parse --short HEAD))"
		exit 0
	fi
	kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break
	sleep 0.5
done
tail -n 30 "$LOG_FILE" >&2
die "cptr did not come up; see $LOG_FILE"
