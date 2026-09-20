"""Tolerant, read-only parsers for the netwatch data files.

Contract: `schema/netwatch-data.md`. Nothing here writes, nothing raises on bad
input, and `unknown` is never coerced to a good value. Unrecognised fields,
markers and targets are **counted** (`Drift`) rather than dropped, so a producer
schema change becomes a visible number instead of a silently missing panel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PROBE_FIELDS = frozenset(
    {
        "ts",
        "kind",
        "gw",
        "iface",
        "rtt_ms",
        "loss_pct",
        "media",
        # added by the producer 2026-09-20
        "link",
        "rx_mbps",
        "saturated",
        "peer",
        "peer_ms",
        "peer_loss_pct",
    }
)
SPEED_FIELDS = frozenset({"ts", "kind", "dl_mbps", "ul_mbps", "ping_ms", "server"})
CSV_TARGETS = frozenset({"gw", "wire", "wl", "net"})
CSV_MARKERS = frozenset(
    {
        "start",
        "stop",
        "loss",
        "burst_start",
        "burst_end",
        "link_change",
        "orbi",
        "iferrs",
    }
)
# The dashboard reads only these keys from ~/.config/netwatch/config.
# NTFY_TOPIC is deliberately absent and must never be read or emitted.
CONFIG_WHITELIST = frozenset(
    {
        "RTT_WARN_MS",
        "RTT_WARN_CONSEC",
        "DL_WARN_MBPS",
        "UL_WARN_MBPS",
        "ALERT_COOLDOWN",
        "NET_PEER",
        # The producer's own switch for gateway-RTT alarms. Read so /healthz can
        # report that RTT alerting is off and why RTT is shown but never moves
        # status (schema §7): it is the config-level form of that decision.
        "RTT_ALERT",
    }
)


@dataclass
class Drift:
    """Everything the parser saw that the schema does not describe."""

    unknown_fields: list[str] = field(default_factory=list)
    unknown_markers: list[str] = field(default_factory=list)
    unknown_targets: list[str] = field(default_factory=list)
    unknown_kinds: list[str] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def count(self) -> int:
        return (
            len(self.unknown_fields)
            + len(self.unknown_markers)
            + len(self.unknown_targets)
            + len(self.unknown_kinds)
            + self.bad_lines
        )


def parse_event(line: str, drift: Drift | None = None) -> dict | None:
    """Parse one `events.jsonl` line. Returns the record or None."""
    if not line.strip():
        return None
    try:
        rec = json.loads(line)
    except (ValueError, TypeError):
        if drift:
            drift.bad_lines += 1
        return None
    if not isinstance(rec, dict):
        if drift:
            drift.bad_lines += 1
        return None
    kind = rec.get("kind")
    known = {"probe": PROBE_FIELDS, "speed": SPEED_FIELDS}.get(kind)
    if drift:
        if known is None:
            drift.unknown_kinds.append(str(kind))
        else:
            drift.unknown_fields.extend(k for k in rec if k not in known)
    return rec


def parse_csv_line(line: str, drift: Drift | None = None) -> tuple[str, dict] | None:
    """Parse one `gateway_rtt.csv` line.

    Returns `("marker", {...})`, `("sample", {...})`, `("header", {})` or None.
    A sample with empty `rtt_ms` is loss, and carries `loss=True`.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return None
    if line.startswith("ts_iso,"):
        return ("header", {})
    if line.startswith("#"):
        parts = line.split(maxsplit=3)
        marker = parts[1] if len(parts) > 1 else ""
        if drift and marker not in CSV_MARKERS:
            drift.unknown_markers.append(marker)
        return ("marker", {"type": marker, "raw": line})
    cols = line.split(",")
    if len(cols) != 4:
        if drift:
            drift.bad_lines += 1
        return None
    ts_iso, unixtime, target, rtt = cols
    if drift and target not in CSV_TARGETS:
        drift.unknown_targets.append(target)
    return (
        "sample",
        {
            "ts_iso": ts_iso,
            "unixtime": float(unixtime) if unixtime else None,
            "target": target,
            "rtt_ms": float(rtt) if rtt else None,
            "loss": not rtt,
        },
    )


IFERR_FIELDS = frozenset({"iface", "ierrs", "oerrs", "coll", "rx_bytes", "tx_bytes"})


def parse_iferrs(marker: dict, drift: Drift | None = None) -> dict | None:
    """Payload of an `# iferrs <ts> {json}` marker, or None.

    Raw counters are returned untouched (the schema says the *dashboard* owns
    deltas); unrecognised keys are counted as drift, never dropped.
    """
    parts = marker.get("raw", "").split(maxsplit=3)
    if len(parts) < 4:
        if drift:
            drift.bad_lines += 1
        return None
    try:
        payload = json.loads(parts[3])
    except (ValueError, TypeError):
        if drift:
            drift.bad_lines += 1
        return None
    if not isinstance(payload, dict):
        if drift:
            drift.bad_lines += 1
        return None
    if drift:
        drift.unknown_fields.extend(
            f"iferrs.{k}" for k in payload if k not in IFERR_FIELDS
        )
    return payload


def iface_error_deltas(prev: dict | None, cur: dict) -> dict:
    """Counter deltas, treating any decrease as a reset -> 0 (schema §8).

    Byte counters and error counters are handled identically: a reboot or
    interface bounce must read as "no new errors", never as a huge negative.
    """
    keys = ("ierrs", "oerrs", "coll", "rx_bytes", "tx_bytes")
    if not prev:
        return dict.fromkeys(keys, 0)
    return {k: max(0, cur.get(k, 0) - prev.get(k, 0)) for k in keys}


def parse_config(text: str) -> dict[str, str]:
    """Whitelisted keys only. NTFY_TOPIC can never appear in the result."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in CONFIG_WHITELIST:
            out[key] = value.strip().strip('"').strip("'")
    return out


def parse_streak(text: str) -> int | None:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def parse_last_alert(text: str) -> dict[str, int]:
    """`kind epoch` lines -> {kind: epoch}. Unparseable lines are skipped."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


def media_is_gigabit(media: object) -> bool:
    """Unknown media is NOT gigabit (schema §2). Absent/None -> False."""
    return isinstance(media, str) and "1000bas" in media.lower()


def derive_status(
    *,
    loss_pct: float | None,
    link_ok: bool | None,
    forwarded_rtt_ms: float | None,
    forwarded_warn_ms: float,
    probe_age_s: float | None,
    probe_stale_s: float,
    en0_errors: int | None = None,
    rtt_streak: int | None = None,
    gw_rtt_ms: float | None = None,
) -> str:
    """Status from the health signals only.

    `gw_rtt_ms` is accepted **for display** and deliberately ignored: gateway
    ICMP measures the router's control-plane CPU, not the path (schema §7), so
    it must never move the status. Pinned by a test.
    """
    if loss_pct is None and link_ok is None and probe_age_s is None:
        return "unknown"
    if probe_age_s is not None and probe_age_s > probe_stale_s:
        return "crit"
    if loss_pct is not None and loss_pct > 0:
        return "crit"
    if link_ok is False:
        return "crit"
    if en0_errors:
        return "crit"
    if forwarded_rtt_ms is not None and forwarded_rtt_ms > forwarded_warn_ms:
        return "warn"
    if rtt_streak:
        return "warn"
    return "ok"
