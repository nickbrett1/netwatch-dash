#!/usr/bin/env python3
"""Network RTT logger (first-hop diagnosis for the Orbi RBRE960 degradation).

Samples several targets on independent schedules so a latency burst can be
localised:

  gw   192.168.1.1   1s   the Orbi router (first hop)
  wire 192.168.1.2   1s   a wired peer (NAS) - same switching fabric as this Mac
  wl   192.168.1.14  5s   a wireless peer (5 GHz) - crosses the air
  net  1.1.1.1       5s   a public target - crosses the WAN

Interpretation:
  gw spikes but wire stays flat  -> problem is at/above the router's LAN port
                                    (router CPU, WAN, or mesh backhaul)
  gw AND wire spike together     -> problem is on the local wired segment
  only wl spikes                 -> Wi-Fi airtime, not the wired path

A '# orbi ...' line is appended every 60s with the router's own view
(internet status, satellite count, device count).

CSV columns: ts_iso,unixtime,target,rtt_ms   (rtt_ms empty = timeout/loss)
Marker lines start with '#': burst_start/burst_end, loss, link_change, orbi,
iferrs (cumulative en0 Ierrs/Oerrs/Coll + rx/tx bytes, raw not delta).

Usage:
  python3 gwping.py                  # run forever
  python3 gwping.py --duration 3600  # run for an hour
  python3 gwping.py --report         # summarize the log
"""
import argparse
import base64
import datetime as dt
import json
import os
import signal
import subprocess
import time
import urllib.request

GW = "192.168.1.1"
TARGETS = [
    ("gw",   "192.168.1.1", 1),
    ("wire", "192.168.1.2", 1),
    ("wl",   "192.168.1.14", 5),
    ("net",  "1.1.1.1",     5),
]
BURST_MS = 20.0
ORBI_EVERY = 60
LOG = os.path.expanduser("~/netwatch/gateway_rtt.csv")
CREDS = os.path.expanduser("~/netwatch/orbi.env")

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def ping_once(host, wait_ms=900):
    """Return RTT in ms, or None on loss/timeout."""
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", str(wait_ms), "-t", "2", host],
            capture_output=True, text=True, timeout=wait_ms / 1000 + 1.5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if "time=" in line:
            try:
                return float(line.split("time=")[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


def media_of(iface="en0"):
    try:
        out = subprocess.run(["ifconfig", iface], capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            if "media:" in line:
                return line.split("media:", 1)[1].strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "?"


def iface_counters(iface="en0"):
    """Cumulative en0 counters from `netstat -ib`, or None.

    Values are raw since-boot totals, deliberately NOT deltas: the dashboard
    owns delta derivation and reset handling (schema §8). Failing to read them
    is normal (missing interface, sandboxed runner) and must never raise.
    """
    try:
        out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return None
    best = None
    for line in out.stdout.splitlines()[1:]:
        p = line.split()
        # Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        if len(p) < 11 or p[0] != iface:
            continue
        try:
            vals = [int(x) for x in (p[4], p[5], p[6], p[7], p[8], p[9], p[10])]
        except ValueError:
            continue
        row = dict(zip(("ipkts", "ierrs", "ibytes", "opkts", "oerrs", "obytes", "coll"), vals))
        # Prefer the cumulative link row (largest packet count).
        if best is None or row["ipkts"] > best["ipkts"]:
            best = row
    if best is None:
        return None
    return {
        "iface": iface,
        "ierrs": best["ierrs"],
        "oerrs": best["oerrs"],
        "coll": best["coll"],
        "rx_bytes": best["ibytes"],
        "tx_bytes": best["obytes"],
    }


def read_creds():
    user = pw = None
    try:
        with open(CREDS) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ORBI_USER="):
                    user = line.split("=", 1)[1]
                elif line.startswith("ORBI_PASS="):
                    pw = line.split("=", 1)[1]
    except OSError:
        return None
    return (user, pw) if user and pw else None


def orbi_status():
    """Router's own dashboard JSON, or None."""
    creds = read_creds()
    if not creds:
        return None
    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    req = urllib.request.Request(
        f"http://{GW}/ajax/basicStatus.cgi?{int(time.time())}",
        data=b"", method="POST",
        headers={"Authorization": f"Basic {token}", "Referer": f"http://{GW}/DashBoard.htm"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_log(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("ts_iso,unixtime,target,rtt_ms\n")


def run(duration=None):
    ensure_log(LOG)
    log = open(LOG, "a", buffering=1)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    start = time.time()
    tick = 0
    in_burst = False
    burst_peak = 0.0
    last_media = media_of()

    log.write(f"# start pid={os.getpid()} targets={[(n, h, i) for n, h, i in TARGETS]}\n")
    while _running:
        if duration and time.time() - start > duration:
            break
        now = dt.datetime.now()
        gw_rtt = None

        for name, host, every in TARGETS:
            if tick % every:
                continue
            rtt = ping_once(host)
            log.write(f"{now.isoformat(timespec='milliseconds')},{now.timestamp():.3f},"
                      f"{name},{'' if rtt is None else f'{rtt:.3f}'}\n")
            if name == "gw":
                gw_rtt = rtt
            if rtt is None:
                log.write(f"# loss {now.isoformat(timespec='seconds')} {name}\n")

        if gw_rtt is not None and gw_rtt > BURST_MS:
            if not in_burst:
                in_burst, burst_peak = True, gw_rtt
                log.write(f"# burst_start {now.isoformat(timespec='seconds')} {gw_rtt:.1f}ms\n")
            else:
                burst_peak = max(burst_peak, gw_rtt)
        elif in_burst:
            in_burst = False
            log.write(f"# burst_end {now.isoformat(timespec='seconds')} peak={burst_peak:.1f}ms\n")

        if tick % ORBI_EVERY == 0:
            st = orbi_status()
            log.write(f"# orbi {now.isoformat(timespec='seconds')} "
                      f"{json.dumps(st) if st else 'unavailable'}\n")
            ctr = iface_counters()
            if ctr is not None:
                log.write(f"# iferrs {now.isoformat(timespec='seconds')} {json.dumps(ctr)}\n")

        m = media_of()
        if m != last_media:
            log.write(f"# link_change {now.isoformat(timespec='seconds')} {last_media} -> {m}\n")
            last_media = m

        tick += 1
        time.sleep(max(0.0, 1.0 - (time.time() - now.timestamp())))

    log.write(f"# stop {dt.datetime.now().isoformat(timespec='seconds')}\n")
    log.close()


def report(path=None, tail_bursts=15):
    path = path or LOG
    if not os.path.exists(path):
        print(f"no log at {path}")
        return
    series = {}
    with open(path) as f:
        lines = f.readlines()
    for line in lines:
        if line.startswith("#") or line.startswith("ts_iso"):
            continue
        p = line.rstrip("\n").split(",")
        if len(p) != 4:
            continue
        try:
            rtt = float(p[3])
        except ValueError:
            rtt = None
        series.setdefault(p[2], []).append((p[0], rtt))

    print(f"# log: {path}")
    for name, s in series.items():
        vals = sorted(v for _, v in s if v is not None)
        if not vals:
            continue
        lost = len(s) - len(vals)

        def pct(p):
            return vals[min(len(vals) - 1, int(p / 100 * len(vals)))]
        print(f"\n{name:>5}: n={len(s)} loss={lost} avg={sum(vals)/len(vals):.1f} "
              f"min={vals[0]:.1f} p50={pct(50):.1f} p90={pct(90):.1f} "
              f"p99={pct(99):.1f} max={vals[-1]:.1f}")
        for lo, hi, label in [(0, 4, "<=4ms"), (4, 20, "4-20ms"),
                              (20, 50, "20-50ms"), (50, 1e9, ">50ms")]:
            c = sum(1 for v in vals if lo < v <= hi)
            print(f"    {label:>8}: {c:6d}  ({100*c/len(vals):5.1f}%)")

    bursts = [l.strip() for l in lines if l.startswith("# burst_start")]
    ends = [l.strip() for l in lines if l.startswith("# burst_end")]
    print(f"\ngateway bursts: {len(bursts)} (open = one ongoing now)")
    for b, e in zip(bursts[-tail_bursts:], ends[-tail_bursts:]):
        print("  ", b, "->", e.split("peak=")[-1] if e else "(open)")
    if len(bursts) > tail_bursts:
        print(f"   ... {len(bursts)-tail_bursts} earlier bursts omitted")

    orbi = [l.strip() for l in lines if l.startswith("# orbi")]
    if orbi:
        print("\nlast router status:")
        for l in orbi[-3:]:
            print("  ", l)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=None)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    report() if args.report else run(args.duration)


if __name__ == "__main__":
    main()
