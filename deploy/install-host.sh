#!/usr/bin/env bash
# install-host.sh — reconcile this repo's launchd jobs onto THIS host.
#
# Idempotent. Safe to re-run. Does not start the dashboard unless asked.
#
#   bash deploy/install-host.sh --dry-run     # show what would change
#   bash deploy/install-host.sh               # install/update producer files + plists, no start
#   bash deploy/install-host.sh --start       # also bootstrap the launcher + start the dashboard
#
# Producers are installed to STABLE PATHS and are never exec'd from `current/`:
# a bad release cannot take alerting down (schema §4). Both halves of that live
# here — the files the jobs run, and the jobs — so a host is populated and wired
# by one artifact rather than by one artifact and one `scp`. The dashboard is a
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

# --- the producer files ------------------------------------------------------
# The plists name stable paths; nothing put the files there. Copying them was a
# manual `scp` until now, which is why a host could be fully wired and still be
# running a producer someone copied over months earlier: the drift report would
# say so, and nothing could act on it.
#
# Copies are made INTO the stable paths, never exec'd out of `current/` (§4), and
# a file that is replaced is kept beside itself first — these are on the alerting
# path and an overwrite there should be reversible without a backup habit.
#
# Where each goes is `netwatch_dash.buildinfo.INSTALL_PATHS`, the same table
# `/healthz` compares against, asked of this payload's own interpreter rather
# than restated in shell: two copies of the mapping is two things to be wrong,
# and the drift count would then be measured at a path nothing was installed to.
# In a checkout there is no payload, so PATH's python3 answers — and it has to be
# able to import the package, because the table is the package's.
PAYLOAD_PY="$REPO/../../python/bin/python3"
[ -x "$PAYLOAD_PY" ] || PAYLOAD_PY="$(command -v python3 || true)"
TS="$(date +%Y%m%d-%H%M%S)"
GWPING_REPLACED=0

echo "-- producer files (from $REPO/producers) --"
if [ ! -d "$REPO/producers" ]; then
  echo "! no producers/ beside this script — nothing to install" >&2
elif [ -z "$PAYLOAD_PY" ]; then
  echo "! no python3 to read the install-path table from netwatch_dash.buildinfo" >&2
  echo "  run this from a payload, or 'pip install -e .' first" >&2
  exit 1
else
  if ! map="$("$PAYLOAD_PY" - "$REPO/producers" "$HOME" <<'PY'
import sys
from pathlib import Path

from netwatch_dash.buildinfo import installable_producers

for name, dest in installable_producers(Path(sys.argv[1]), Path(sys.argv[2])):
    print(f"{name}\t{dest}")
PY
  )"; then
    echo "! could not read the install-path table from netwatch_dash.buildinfo" >&2
    echo "  run this from a payload, or 'pip install -e .' first" >&2
    exit 1
  fi

  while IFS="$(printf '\t')" read -r name dest; do
    [ -n "$name" ] || continue
    src="$REPO/producers/$name"
    if [ ! -f "$src" ]; then
      echo "  - $name (not in this release)"
      continue
    fi
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
      echo "= $name (up to date)"
      continue
    fi
    if [ -f "$dest" ]; then
      echo "~ $name -> $dest (replaced; previous kept as $dest.bak.$TS)"
      if [ "$name" = "gwping.py" ]; then GWPING_REPLACED=1; fi
    else
      echo "+ $name -> $dest"
    fi
    if [ "$DRY" = 1 ]; then continue; fi
    mkdir -p "$(dirname "$dest")"
    if [ -f "$dest" ]; then cp -p "$dest" "$dest.bak.$TS"; fi
    install -m 0755 "$src" "$dest.new.$$"
    mv -f "$dest.new.$$" "$dest"
  done <<EOF
$map
EOF
fi

echo "-- producer jobs (from deploy/plists/, unchanged behaviour) --"
for l in com.nick.netwatch.gwping com.nickbrett.netwatch.probe com.nickbrett.netwatch.speed; do
  install_plist "$HERE/plists/$l.plist" "$l"
done

# Of the producers, only gwping.py is a long-running process. `netwatch` is
# started fresh by its interval jobs, and flapwatch/check_link.sh are not scheduled
# at all, so a replaced file reaches them at their next run; gwping.py would
# otherwise keep executing the code it was started with. This runs *after* the
# jobs are installed, so the only job it can restart is one that exists.
if [ "$GWPING_REPLACED" = 1 ] && [ "$DRY" = 0 ]; then
  if launchctl print "gui/$UID/com.nick.netwatch.gwping" >/dev/null 2>&1; then
    echo "  restarting com.nick.netwatch.gwping to load the new gwping.py"
    launchctl kickstart -k "gui/$UID/com.nick.netwatch.gwping"
  else
    echo "  gwping.py was replaced; its job is not loaded here, so nothing restarted"
  fi
fi

echo "-- new dashboard job (not started unless --start) --"
if [ "$DRY" = 0 ]; then mkdir -p "$DEPLOY_DIR" "$HOME/.config/netwatch-dash"; fi
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
