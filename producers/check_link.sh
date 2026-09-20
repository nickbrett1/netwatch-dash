#!/usr/bin/env bash
# check_link.sh — one-command Fast-Ethernet (100baseT) detector for mac-studio.
#
# Why: on 2026-09-19 a single bad/re-seated patch cable auto-negotiated at
# 100baseT and silently capped the whole path at ~94 Mbps. The signature is
# a download/upload pinned near ~94 Mbps AND a local-only Mac<->NAS transfer
# also ~94 Mbps (which rules out the ISP). Wired != gigabit.
#
# This runs both tests and fails if either is below THRESHOLD.
#
# Usage:  ~/netwatch/check_link.sh
# Exit:   0 = link healthy (>= threshold), 1 = something is capped/slow.
set -u

SPEEDTEST="${SPEEDTEST_BIN:-$HOME/.local/bin/speedtest}"
NAS="${NAS_HOST:-nick@192.168.1.2}"
NAS_PORT="${NAS_SSH_PORT:-2222}"
THRESHOLD_MBPS="${THRESHOLD_MBPS:-200}"     # ~500/560 expected; 200 is a safe floor
LAN_IP="${LAN_IP:-$(ipconfig getifaddr en0)}"
LAN_FILE_MB="${LAN_FILE_MB:-300}"

fail=0

echo "=== en0 link ==="
ifconfig en0 | grep -E "media:|status:" | sed 's/^/  /'

echo
echo "=== WAN (Ookla) ==="
if [[ ! -x "$SPEEDTEST" ]]; then
  echo "  !! Ookla CLI not found at $SPEEDTEST  (try: curl -sSL https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-macosx-universal.tgz | tar xz -C /tmp && cp /tmp/speedtest ~/.local/bin/ && xattr -d com.apple.quarantine ~/.local/bin/speedtest)"
  fail=1
else
  out=$("$SPEEDTEST" --accept-license --accept-gdpr -f json 2>/dev/null)
  dl=$(printf '%s' "$out" | python3 -c 'import sys,json;print(round(json.load(sys.stdin)["download"]["bandwidth"]*8/1e6,1))' 2>/dev/null)
  ul=$(printf '%s' "$out" | python3 -c 'import sys,json;print(round(json.load(sys.stdin)["upload"]["bandwidth"]*8/1e6,1))' 2>/dev/null)
  echo "  DL ${dl:-?} Mbps   UL ${ul:-?} Mbps"
  for m in "$dl" "$ul"; do
    [[ "$m" =~ ^[0-9] ]] && awk -v a="$m" -v t="$THRESHOLD_MBPS" 'BEGIN{exit !(a<t)}' && { echo "  !! below ${THRESHOLD_MBPS} Mbps -> suspect 100baseT cable/port"; fail=1; }
  done
fi

echo
echo "=== LAN Mac -> NAS (raw HTTP, no SSH overhead) ==="
tmp=$(mktemp -d)
trap 'kill "$srv" 2>/dev/null; rm -rf "$tmp"' EXIT
mkfile -n "${LAN_FILE_MB}m" "$tmp/big.bin" 2>/dev/null || dd if=/dev/zero of="$tmp/big.bin" bs=1m count="$LAN_FILE_MB" 2>/dev/null
python3 -m http.server 8000 --directory "$tmp" >/dev/null 2>&1 &
srv=$!
disown "$srv" 2>/dev/null || true
sleep 1
res=$(ssh -p "$NAS_PORT" -o BatchMode=yes "$NAS" \
  "curl -s -o /dev/null -w '%{size_download} %{time_total}' http://${LAN_IP}:8000/big.bin" 2>/dev/null)
bytes=$(printf '%s' "$res" | awk '{print $1}'); secs=$(printf '%s' "$res" | awk '{print $2}')
if [[ -n "${bytes:-}" && "${secs:-0}" != "0" ]]; then
  mbps=$(python3 -c "print(round($bytes*8/1e6/$secs,1))")
  echo "  ${bytes} bytes in ${secs}s = ${mbps} Mbps (gigabit line rate is ~940)"
  awk -v a="$mbps" -v t="$THRESHOLD_MBPS" 'BEGIN{exit !(a<t)}' && { echo "  !! LAN path is capped -> 100baseT link inside the wired run"; fail=1; }
else
  echo "  !! LAN test failed (NAS unreachable at $NAS port $NAS_PORT / no LAN IP?)"
  fail=1
fi

echo
if [[ "$fail" == "0" ]]; then
  echo "RESULT: OK — link looks gigabit end-to-end."
else
  echo "RESULT: CAPPED/SLOW — reseat or replace the cable run before blaming anything else."
fi
exit "$fail"
