#!/usr/bin/env bash
# Runs this fork of cptr (branch deploy) as a systemd service, so it starts on boot and comes back
# if it crashes.
#
#   install.sh [install]                 clone or update, build, write the service and restart it
#   install.sh start|stop|restart        control the service
#   install.sh status                    is it running, and since when
#   install.sh logs                      follow the server's log
#
# First install on a new machine (run it as the user that will own the server, or as root with
# CPTR_USER; it asks for sudo only to write the service):
#   curl -fsSL https://raw.githubusercontent.com/ciberick/computer/deploy/install.sh -o install.sh
#   CPTR_PORT=9741 bash install.sh
# After that it lives in the repo: ~/computer/install.sh restart
#
# Settings (environment variables). Once the service exists its unit is the configuration: a
# setting left unset keeps the value the service already has, so an update never moves the port.
#   CPTR_USER     user that runs the server        (default: you; under sudo, the user who ran it)
#   CPTR_DIR      where the repo lives             (default: ~CPTR_USER/computer)
#   CPTR_BRANCH   branch to install                (default: deploy)
#   CPTR_HOST     address to bind                  (default: 0.0.0.0)
#   CPTR_PORT     port to bind                     (default: 8000)
#   CPTR_EXTRAS   optional extras, comma-separated (default: agents)
#   CPTR_PATH     PATH the server sees, where it finds claude, codex, uv... (default: your PATH)
#   CPTR_DATA_DIR data directory, if not the default ~/.cptr
#   CPTR_SERVICE  service name                     (default: cptr)
set -euo pipefail

SERVICE="${CPTR_SERVICE:-cptr}"
UNIT="/etc/systemd/system/$SERVICE.service"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# A value from the existing unit: unit_get User, unit_env PATH.
unit_get() { [ -f "$UNIT" ] && sed -n "s/^$1=//p" "$UNIT" | head -n1 || true; }
unit_env() { [ -f "$UNIT" ] && sed -n "s/^Environment=\"$1=\(.*\)\"$/\1/p" "$UNIT" | head -n1 || true; }
unit_arg() { [ -f "$UNIT" ] && sed -n "s/^ExecStart=.* --$1 \([^ ]*\).*/\1/p" "$UNIT" | head -n1 || true; }

wait_healthy() {
	local port="$1"
	for _ in $(seq 60); do
		curl -fsS -o /dev/null "http://127.0.0.1:$port/api/health" 2>/dev/null && return 0
		# "activating" covers the pause before Restart=on-failure tries again.
		case "$(systemctl show -p ActiveState --value "$SERVICE")" in failed | inactive) break ;; esac
		sleep 0.5
	done
	as_root journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
	die "cptr did not come up on port $port; see: $0 logs"
}

# --- Day-to-day commands ----------------------------------------------------------------------

cmd="${1:-install}"
case "$cmd" in
start | stop | restart)
	[ -f "$UNIT" ] || die "the $SERVICE service is not installed; run: $0 install"
	as_root systemctl "$cmd" "$SERVICE"
	[ "$cmd" = stop ] || wait_healthy "$(unit_arg port)"
	systemctl status "$SERVICE" --no-pager --lines=0 || true
	exit 0
	;;
status)
	exec systemctl status "$SERVICE" --no-pager
	;;
logs)
	as_root journalctl -u "$SERVICE" -n 100 -f
	exit 0
	;;
install | update) ;;
*) die "unknown command '$cmd' (install, start, stop, restart, status, logs)" ;;
esac

# --- Configuration ----------------------------------------------------------------------------

[ -d /run/systemd/system ] || die "this machine does not run systemd"

SVC_USER="${CPTR_USER:-$(unit_get User)}"
SVC_USER="${SVC_USER:-${SUDO_USER:-$(id -un)}}"
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
[ -n "$SVC_HOME" ] || die "user '$SVC_USER' does not exist"
SVC_GROUP="$(id -gn "$SVC_USER")"

DIR="${CPTR_DIR:-$(unit_get WorkingDirectory)}"
DIR="${DIR:-$SVC_HOME/computer}"
BRANCH="${CPTR_BRANCH:-deploy}"
HOST="${CPTR_HOST:-$(unit_arg host)}"
HOST="${HOST:-0.0.0.0}"
PORT="${CPTR_PORT:-$(unit_arg port)}"
PORT="${PORT:-8000}"
EXTRAS="${CPTR_EXTRAS:-agents}"
DATA_DIR="${CPTR_DATA_DIR:-$(unit_env CPTR_DATA_DIR)}"
REPO_URL="${CPTR_REPO_URL:-https://github.com/ciberick/computer.git}"

# systemd does not load a login shell, so without this the server would not find claude, codex or
# uv and every agent would show as "not found". Your own PATH is the best guess when you are the
# server's user; for another user, its ~/.local/bin, wherever node is, and the system directories.
SVC_PATH="${CPTR_PATH:-$(unit_env PATH)}"
if [ -z "$SVC_PATH" ]; then
	if [ "$(id -un)" = "$SVC_USER" ]; then
		SVC_PATH="$PATH"
	else
		node_dir="$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)")"
		SVC_PATH="$node_dir:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	fi
fi
case ":$SVC_PATH:" in
*":$SVC_HOME/.local/bin:"*) ;;
*) SVC_PATH="$SVC_HOME/.local/bin:$SVC_PATH" ;;
esac

# Everything that touches the repo runs as the server's user, so the files stay theirs.
as_svc() {
	if [ "$(id -un)" = "$SVC_USER" ]; then
		env PATH="$SVC_PATH" "$@"
	elif [ "$(id -u)" -eq 0 ]; then
		runuser -u "$SVC_USER" -- env HOME="$SVC_HOME" PATH="$SVC_PATH" "$@"
	else
		sudo -u "$SVC_USER" -H env PATH="$SVC_PATH" "$@"
	fi
}
cd / # a directory every user can read, in case this runs from root's home

log "Service '$SERVICE': user $SVC_USER, repo $DIR ($BRANCH), $HOST:$PORT"
[ "$SVC_USER" != root ] || log "Note: the server will run as root"

# --- Requirements -----------------------------------------------------------------------------

as_svc sh -c 'command -v git' >/dev/null || die "git is not installed (or not in the PATH of $SVC_USER)"
as_svc sh -c 'command -v npm' >/dev/null ||
	die "Node.js/npm is not in the PATH of $SVC_USER (set CPTR_PATH); the frontend build needs Node 20.19+ or 22.12+"
node_major="$(as_svc node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 20 ] || die "Node $(as_svc node -v) is too old: the frontend build needs Node 20.19+ or 22.12+"
if ! as_svc sh -c 'command -v uv' >/dev/null; then
	log "Installing uv for $SVC_USER"
	as_svc sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

# --- Code -------------------------------------------------------------------------------------

if [ -d "$DIR/.git" ]; then
	log "Updating $DIR ($BRANCH)"
	# Local edits would be silently mixed into the build, or block the pull halfway.
	[ -z "$(as_svc git -C "$DIR" status --porcelain --untracked-files=no)" ] ||
		die "$DIR has local changes; commit or stash them first (git -C $DIR status)"
	as_svc git -C "$DIR" fetch origin "$BRANCH"
	as_svc git -C "$DIR" checkout -q "$BRANCH"
	as_svc git -C "$DIR" merge --ff-only "origin/$BRANCH"
else
	log "Cloning $REPO_URL ($BRANCH) into $DIR"
	as_svc git clone -b "$BRANCH" "$REPO_URL" "$DIR"
fi

# --- Build ------------------------------------------------------------------------------------

log "Installing Python dependencies (extras: $EXTRAS)"
extra_args=()
IFS=',' read -ra extras <<<"$EXTRAS"
for extra in "${extras[@]}"; do
	if [ -n "$extra" ]; then extra_args+=(--extra "$extra"); fi
done
# --locked: install exactly what uv.lock pins, and never rewrite it (that would block the next update).
as_svc uv sync --directory "$DIR" --locked "${extra_args[@]}"

log "Building the frontend"
as_svc npm --prefix "$DIR/cptr/frontend" ci
as_svc npm --prefix "$DIR/cptr/frontend" run build

# --- Service ----------------------------------------------------------------------------------

# A server from before the service existed, started by hand with nohup from this repo, would keep
# the port and the service could never bind it. Other cptr instances are left alone.
old_pids=()
if [ -f "$DIR/cptr.pid" ] && as_svc kill -0 "$(cat "$DIR/cptr.pid")" 2>/dev/null; then
	old_pids+=("$(cat "$DIR/cptr.pid")")
fi
for pid in $(pgrep -u "$SVC_USER" -f "cptr run" || true); do
	[ "$(as_svc readlink "/proc/$pid/cwd" 2>/dev/null)" = "$DIR" ] || continue
	# The service's own process is restarted below, not killed.
	[ "$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)" = "$pid" ] && continue
	old_pids+=("$pid")
done
if [ "${#old_pids[@]}" -gt 0 ]; then
	log "Stopping the server started by hand (pid ${old_pids[*]})"
	as_svc kill "${old_pids[@]}" 2>/dev/null || true
	for _ in $(seq 20); do
		as_svc kill -0 "${old_pids[@]}" 2>/dev/null || break
		sleep 0.5
	done
	as_svc kill -9 "${old_pids[@]}" 2>/dev/null || true
fi
as_svc rm -f "$DIR/cptr.pid"

log "Writing $UNIT"
{
	cat <<EOF
[Unit]
Description=cptr (ciberick/computer, $BRANCH)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_GROUP
WorkingDirectory=$DIR
Environment="HOME=$SVC_HOME"
Environment="PATH=$SVC_PATH"
EOF
	[ -z "$DATA_DIR" ] || echo "Environment=\"CPTR_DATA_DIR=$DATA_DIR\""
	cat <<EOF
ExecStart=$DIR/.venv/bin/cptr run --host $HOST --port $PORT --headless
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
} | as_root tee "$UNIT" >/dev/null

as_root systemctl daemon-reload
as_root systemctl enable --quiet "$SERVICE"
log "Restarting $SERVICE"
as_root systemctl restart "$SERVICE"
wait_healthy "$PORT"

version="$(as_svc "$DIR/.venv/bin/python" -c 'from importlib.metadata import version; print(version("cptr"))')"
log "cptr $version is up on port $PORT ($(as_svc git -C "$DIR" rev-parse --short HEAD))"
echo "Manage it with: $DIR/install.sh start|stop|restart|status|logs"
