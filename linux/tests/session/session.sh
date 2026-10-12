#!/bin/bash
# Runs INSIDE dbus-run-session, started by supervisor.py. Never touches the real desktop.
# Inputs (env): GD_WORK GD_REAL_DBUS GD_WL GD_SOURCES GD_EXTENSION GD_ENABLED_EXTENSIONS GD_LOGDIR GD_SIZE GD_HERE
set -u
fail() { echo "ABORT: $*"; exit 1; }
[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || fail "no session bus"
[ "$DBUS_SESSION_BUS_ADDRESS" != "${GD_REAL_DBUS:-}" ] || fail "would run on the real session bus"
case "$GD_WORK" in /tmp/gd-*) ;; *) fail "work dir not under /tmp/gd-";; esac
case "$DBUS_SESSION_BUS_ADDRESS" in *"$GD_WORK/"*) ;; *) fail "session bus not inside $GD_WORK";; esac
for v in XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_RUNTIME_DIR HOME TMPDIR; do
  case "${!v}" in "$GD_WORK"/*) ;; *) fail "$v not isolated";; esac
done
unset DISPLAY GDK_BACKEND ELECTRON_PA_APP_NAME XAUTHORITY GNOME_SETUP_DISPLAY SESSION_MANAGER
export WAYLAND_DISPLAY=$GD_WL
echo "session bus: $DBUS_SESSION_BUS_ADDRESS"
if [ -n "${GD_SYSBUS_CONF:-}" ]; then
  # Private, empty system bus: keeps gnome-shell away from the host's logind/GDM/NetworkManager.
  dbus-daemon --config-file="$GD_SYSBUS_CONF" --fork --nopidfile --print-address >/dev/null || fail "private system bus"
fi
echo "system bus: ${DBUS_SYSTEM_BUS_ADDRESS:-<host>}"
echo "IBUS_COMPONENT_PATH=${IBUS_COMPONENT_PATH:-<unset>}"

gs() { gsettings set "$@" || fail "gsettings set $*"; }
gs org.gnome.desktop.input-sources sources "$GD_SOURCES"
gs org.gnome.shell enabled-extensions "${GD_ENABLED_EXTENSIONS:-['$GD_EXTENSION']}"
gs org.gnome.shell disable-user-extensions false
gs org.gnome.desktop.interface enable-animations false
gs org.gnome.shell welcome-dialog-last-shown-version '999'
gs org.gnome.desktop.session idle-delay 'uint32 0'
gs org.gnome.desktop.screensaver lock-enabled false
echo "sources: $(gsettings get org.gnome.desktop.input-sources sources)"

gnome-shell --headless --wayland --wayland-display "$GD_WL" --virtual-monitor "$GD_SIZE" ${GD_SHELL_ARGS:-} \
  > "$GD_LOGDIR/shell.log" 2>&1 &
SHELL_PID=$!
echo "$SHELL_PID" > "$GD_WORK/shell.pid"
for _ in $(seq 1 120); do [ -S "$XDG_RUNTIME_DIR/$GD_WL" ] && break; sleep 0.25; done
[ -S "$XDG_RUNTIME_DIR/$GD_WL" ] || fail "no wayland socket"
echo "shell up pid=$SHELL_PID"
/usr/bin/python3 "$GD_HERE/ctl.py" "$GD_WORK/ctl.sock" > "$GD_LOGDIR/ctl.log" 2>&1
echo "ctl exited rc=$?; stopping shell"
kill "$SHELL_PID" 2>/dev/null
for _ in $(seq 1 20); do kill -0 "$SHELL_PID" 2>/dev/null || break; sleep 0.1; done
kill -9 "$SHELL_PID" 2>/dev/null
exit 0
