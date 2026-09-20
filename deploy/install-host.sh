#!/usr/bin/env bash
# install-host.sh — reconcile this repo's launchd jobs onto THIS host.
#
# Idempotent. Safe to re-run. Does not start the dashboard unless asked.
#
#   bash deploy/install-host.sh --dry-run     # show what would change
#   bash deploy/install-host.sh               # install/update plists, no start
#   bash deploy/install-host.sh --start       # also bootstrap the launcher + start the dashboard
#
# Producers are installed to STABLE PATHS and are never exec'd from `current/`:
# a bad release cannot take alerting down (schema §4). The dashboard is a
# LaunchAgent (user), not a LaunchDaemon — auto-login is confirmed enabled.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/.local/share/netwatch-dash}"
LA="$HOME/Library/LaunchAgents"
DRY=0
START=0
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --start) START=1 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

run() { if [ "$DRY" = 1 ]; then echo "  would: $*"; else "$@"; fi; }
# Templatise the home path so the plists are reproducible off this machine.
templ() { sed "s|/Users/nick|$HOME|g" "$1"; }

install_plist() {  # <source-file|-> <label>
  local src="$1" label="$2" dest="$LA/$2.plist"
  if [ -f "$dest" ] && templ "$src" | diff -q - "$dest" >/dev/null 2>&1; then
    echo "= $label (up to date)"; return 0
  fi
  echo "~ $label -> $dest"
  if [ "$DRY" = 1 ]; then return 0; fi
  mkdir -p "$LA"
  templ "$src" > "$dest.new.$$"
  mv -f "$dest.new.$$" "$dest"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$dest"
}

echo "installing host wiring for $USER@$HOSTNAME"
echo "-- producer jobs (from deploy/plists/, unchanged behaviour) --"
for l in com.nick.netwatch.gwping com.nickbrett.netwatch.probe com.nickbrett.netwatch.speed; do
  install_plist "$HERE/plists/$l.plist" "$l"
done
echo "note: producers must already exist at ~/netwatch/ and ~/.local/bin/ (see producers/README.md)"

echo "-- new dashboard job (not started unless --start) --"
mkdir -p "$DEPLOY_DIR" "$HOME/.config/netwatch-dash"
if [ ! -f "$HOME/.config/netwatch-dash/env" ] && [ "$DRY" = 0 ]; then
  cat > "$HOME/.config/netwatch-dash/env" <<EON
NETWATCH_DASH_BIND=100.77.144.14:8791
NETWATCH_DASH_STATE=$HOME/.local/state/netwatch
NETWATCH_DASH_GWCSV=$HOME/netwatch/gateway_rtt.csv
NETWATCH_DASH_CONFIG=$HOME/.config/netwatch/config
NETWATCH_DASH_TZ=America/New_York
EON
  echo "+ wrote $HOME/.config/netwatch-dash/env"
fi

# Cold start: the launcher must be put down once before launchd can supervise it.
LAUNCHER="$DEPLOY_DIR/fetch-launch.sh"
if [ ! -x "$LAUNCHER" ]; then
  echo "! $LAUNCHER missing — bootstrap it first:"
  echo "  curl -fsSL https://github.com/nickbrett1/netwatch-dash/releases/latest/download/fetch-launch.sh -o '$LAUNCHER' && chmod +x '$LAUNCHER'"
fi

for label in com.nick.netwatch.dash com.nick.netwatch.dash.refresh; do
  dest="$LA/$label.plist"
  if [ -f "$dest" ]; then echo "= $label (up to date)"; continue; fi
  echo "~ $label -> $dest"
  [ "$DRY" = 1 ] && continue
  if [ "$label" = com.nick.netwatch.dash ]; then
    cat > "$dest.new.$$" <<EOP
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$LAUNCHER</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DEPLOY_DIR/dash.log</string>
  <key>StandardErrorPath</key><string>$DEPLOY_DIR/dash.err</string>
</dict></plist>
EOP
  else
    cat > "$dest.new.$$" <<EOP
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>launchctl kickstart -k gui/\$UID/com.nick.netwatch.dash</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
</dict></plist>
EOP
  fi
  mv -f "$dest.new.$$" "$dest"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$dest"
done

if [ "$START" = 1 ] && [ "$DRY" = 0 ]; then
  echo "-- starting the dashboard via the launcher --"
  launchctl kickstart -k "gui/$UID/com.nick.netwatch.dash"
fi
echo "done. verify: curl -sS http://100.77.144.14:8791/healthz"
