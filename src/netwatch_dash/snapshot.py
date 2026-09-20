"""One read of the live state, and the status that comes out of it.

Every endpoint renders a piece of the same read, so the read lives here rather
than in the routes: two endpoints answering from two reads is how a page ends up
showing a probe the status never saw.

What this deliberately does **not** do:

* It does not read the RTT CSV. It is ~8.4 MB/day and must never be parsed per
  request (schema §3); its reader is a tailer and a rollup (Phase 3).
* It does not pass `rtt_streak` into the status. The streak counts consecutive
  over-threshold *gateway* probes, and gateway RTT is not a health input
  (schema §7) — the producer's own `RTT_ALERT=off` default says the same thing.
  The streak is still reported, as a diagnostic with its provenance (schema §7's
  table row, "Gateway RTT — diagnostic only").
* It computes no alerts and sends none (memo v3 D5). Alerting is `netwatch`'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import events
from .parse import (
    derive_status,
    media_is_gigabit,
    parse_config,
    parse_last_alert,
    parse_streak,
    speed_is_stale,
)
from .settings import Settings

# Status inputs the contract defines and nothing reads yet. Named in every answer
# so that "ok" cannot be read as "everything was checked".
UNREAD_STATUS_INPUTS = (
    "forwarded-path RTT (csv `net`/`wire` rows): the csv rollup, Phase 3",
    "en0 error counters (csv `# iferrs` markers): the csv rollup, Phase 3",
)

# Endpoints that exist by name and not by behaviour, and why.
NOT_IMPLEMENTED = {
    "/api/link": "needs the csv `# link_change` markers (Phase 3)",
    "/api/localise": "needs the csv targets and `# iferrs` deltas (Phase 3)",
    "/api/incidents": "needs the csv loss/burst markers and .last_alert (Phase 3)",
}

# The fields a probe view carries: the schema's field set and nothing else, so a
# field the producer adds shows up as drift rather than as a silent extra key.
PROBE_FIELDS = (
    "ts",
    "gw",
    "iface",
    "rtt_ms",
    "loss_pct",
    "media",
    "link",
    "rx_mbps",
    "saturated",
    "peer",
    "peer_ms",
    "peer_loss_pct",
)
SPEED_FIELDS = ("ts", "dl_mbps", "ul_mbps", "ping_ms", "server")


def _number(value: object) -> float | None:
    """A JSON number as a float, or None.

    `bool` is excluded deliberately: `True` is an `int` in Python, and a
    saturated flag read as a loss figure is exactly the kind of quiet coercion
    the contract forbids (schema §6).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _config_number(config: dict[str, str], key: str) -> float | None:
    value = config.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _read_text(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, "not present"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def link_ok(probe: dict | None) -> bool | None:
    """Whether the link is at gigabit, from the probe's `media`.

    Three-valued, because "no media recorded" and "media recorded as something
    else" are different facts: the first is unknown and the second is the
    renegotiation canary (schema §2, §7). Absent `media` is never 1000baseT *and*
    never a fault — it is unknown.
    """
    media = probe.get("media") if probe else None
    if not isinstance(media, str) or not media:
        return None
    return media_is_gigabit(media)


@dataclass
class Snapshot:
    settings: Settings
    now: datetime
    events: events.EventLog
    config: dict[str, str]
    config_error: str | None
    streak: int | None
    streak_error: str | None
    last_alert: dict[str, int]
    last_alert_error: str | None

    # --- the measurements ---------------------------------------------------

    def age_s(self, record: dict | None) -> float | None:
        """How long ago a record was written, or None if it cannot be aged.

        Clamped at 0: a record from the future is a clock that disagrees with
        this host, and a negative age would make "stale" arithmetic quietly
        wrong. Zero says "now, as far as this process can tell".
        """
        timestamp = events.timestamp(record) if record else None
        if timestamp is None:
            return None
        return max(0.0, (self.now - timestamp).total_seconds())

    @property
    def probe(self) -> dict | None:
        return self.events.newest_probe

    @property
    def speed(self) -> dict | None:
        return self.events.newest_speed

    @property
    def probe_age_s(self) -> float | None:
        return self.age_s(self.probe)

    @property
    def speed_age_s(self) -> float | None:
        return self.age_s(self.speed)

    def view(self, record: dict | None, fields: tuple[str, ...]) -> dict | None:
        """A record as the schema describes it, with its age.

        Absent fields are `None`, not omitted and not zero (schema §6): the page
        then has to decide what to do about unknown, rather than being handed a
        value that looks measured.
        """
        if record is None:
            return None
        rendered = {name: record.get(name) for name in fields}
        rendered["age_s"] = self.age_s(record)
        return rendered

    def probe_views(self, limit: int) -> list[dict]:
        """The most recent probes, oldest first."""
        if limit <= 0:
            return []
        return [self.view(r, PROBE_FIELDS) for r in self.events.probes[-limit:]]

    def speed_views(self, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        return [self.view(r, SPEED_FIELDS) for r in self.events.speeds[-limit:]]

    # --- the status ---------------------------------------------------------

    def status_inputs(self) -> dict:
        """Exactly the arguments `derive_status` is called with.

        Returned to callers as well as used internally, so the answer to "why is
        it that colour" is the same object as the decision that produced it.
        """
        probe = self.probe
        speed = self.speed
        return {
            "loss_pct": _number(probe.get("loss_pct")) if probe else None,
            "link_ok": link_ok(probe),
            # The csv, Phase 3. `forwarded_warn_ms` is unused while the reading is
            # absent, and says so rather than being given a plausible number.
            "forwarded_rtt_ms": None,
            "forwarded_warn_ms": 0.0,
            "probe_age_s": self.probe_age_s,
            "probe_stale_s": self.settings.probe_stale_s,
            # The csv `# iferrs` deltas, Phase 3.
            "en0_errors": None,
            # Not passed, on purpose: see the module docstring.
            "rtt_streak": None,
            # Accepted by `derive_status` for display and ignored by design.
            "gw_rtt_ms": _number(probe.get("rtt_ms")) if probe else None,
            "dl_mbps": _number(speed.get("dl_mbps")) if speed else None,
            "ul_mbps": _number(speed.get("ul_mbps")) if speed else None,
            "dl_warn_mbps": _config_number(self.config, "DL_WARN_MBPS"),
            "ul_warn_mbps": _config_number(self.config, "UL_WARN_MBPS"),
            "speed_age_s": self.speed_age_s,
            "speed_stale_s": self.settings.speed_stale_s,
        }

    @property
    def status(self) -> str:
        return derive_status(**self.status_inputs())

    def status_reason(self) -> list[str]:
        """Why the status is what it is, and what it is not looking at.

        Every `unknown` has a cause that is worth printing, and every `ok` is
        qualified by the inputs that are missing — an unqualified green light is
        the failure mode this list exists to prevent.
        """
        reasons: list[str] = []
        if self.events.error:
            reasons.append(f"events.jsonl: {self.events.error}")
        elif not self.events.probes:
            reasons.append(
                "no probe record with a usable timestamp in the "
                f"{events.DEFAULT_TAIL_BYTES}-byte tail of events.jsonl"
            )

        probe = self.probe
        age = self.probe_age_s
        if probe and age is not None and age > self.settings.probe_stale_s:
            reasons.append(
                f"the newest probe is {age:.0f}s old "
                f"(stale after {self.settings.probe_stale_s:.0f}s)"
            )
        loss = self.status_inputs()["loss_pct"]
        if loss:
            reasons.append(f"loss {loss}% on the newest probe")
        link = link_ok(probe)
        if link is False:
            reasons.append(
                f"link media is {probe.get('media')!r}, which is not 1000baseT"
            )
        if link is None and probe:
            reasons.append("no `media` recorded on the newest probe: link rate unknown")

        self._throughput_reasons(reasons)

        if self.config_error:
            reasons.append(
                f"config {self.config_error} - thresholds are the defaults, not the host's"
            )
        if "RTT_ALERT" in self.config:
            reasons.append(
                f"RTT_ALERT={self.config['RTT_ALERT']} on the host: gateway RTT is "
                "diagnostic only and never moves the status (schema §7)"
            )
        if self.streak_error:
            reasons.append(f".rtt_streak: {self.streak_error}")
        if self.last_alert_error:
            reasons.append(f".last_alert: {self.last_alert_error}")

        reasons.extend(f"not read yet - {item}" for item in UNREAD_STATUS_INPUTS)
        return reasons

    def _throughput_reasons(self, reasons: list[str]) -> None:
        speed = self.speed
        if not speed:
            reasons.append("no speed measurement in the window")
            return
        age = self.speed_age_s
        if speed_is_stale(age, self.settings.speed_stale_s):
            reasons.append(
                f"the newest speed measurement is {age:.0f}s old, so it does not "
                "speak for the link now"
            )
            return
        for key, name in (("DL_WARN_MBPS", "download"), ("UL_WARN_MBPS", "upload")):
            if self.config.get(key) is not None and _config_number(
                self.config, key
            ) is None:
                reasons.append(f"{key} is not a number, so {name} has no threshold")

    # --- the evidence -------------------------------------------------------

    def sources(self) -> dict:
        """Where each answer came from, and what happened when it was read."""
        return {
            "events": {
                "path": str(self.events.path),
                "error": self.events.error,
                "size": self.events.size,
                "bytes_read": self.events.bytes_read,
                "lines": self.events.lines,
                "truncated": self.events.truncated,
            },
            "config": {"path": str(self.settings.config_path), "error": self.config_error},
            "streak": {"path": str(self.settings.streak_path), "error": self.streak_error},
            "last_alert": {
                "path": str(self.settings.last_alert_path),
                "error": self.last_alert_error,
            },
            "csv": {
                "path": str(self.settings.csv_path),
                "read": False,
                "reason": "never parsed per request (schema §3); the csv rollup is Phase 3",
            },
        }

    def summary(self) -> dict:
        return {
            "status": self.status,
            "status_reason": self.status_reason(),
            "status_inputs": self.status_inputs(),
            "now": _iso(self.now),
            "probe": self.view(self.probe, PROBE_FIELDS),
            "speed": self.view(self.speed, SPEED_FIELDS),
            "streak": self.streak,
            "last_alert": self.last_alert,
            "config": self.config,
            "config_error": self.config_error,
            "drift": self.events.drift.as_dict(),
            "sources": self.sources(),
            "tz": self.settings.tz,
            "not_read": list(UNREAD_STATUS_INPUTS),
        }

    def collection(self, kind: str, limit: int) -> dict:
        """The shape `/api/probe` and `/api/speed` both return."""
        views = self.probe_views(limit) if kind == "probe" else self.speed_views(limit)
        held = len(self.events.probes if kind == "probe" else self.events.speeds)
        return {
            kind + "s": views,
            "returned": len(views),
            # How much the bounded tail actually held: "no data" and "no data in
            # the window I read" are different answers, and a page that cannot
            # tell them apart will draw an empty chart over a full file.
            "in_window": held,
            "limit": limit,
            "truncated": self.events.truncated,
            "drift": self.events.drift.as_dict(),
            "sources": self.sources(),
        }


def build(settings: Settings, now: datetime | None = None) -> Snapshot:
    """One read of everything Phase 2 can reach."""
    now = now or datetime.now(UTC)
    config_text, config_error = _read_text(settings.config_path)
    streak_text, streak_error = _read_text(settings.streak_path)
    alert_text, alert_error = _read_text(settings.last_alert_path)
    return Snapshot(
        settings=settings,
        now=now,
        events=events.read(settings.events_path),
        config=parse_config(config_text) if config_text is not None else {},
        config_error=config_error,
        streak=parse_streak(streak_text) if streak_text is not None else None,
        streak_error=streak_error,
        last_alert=parse_last_alert(alert_text) if alert_text is not None else {},
        last_alert_error=alert_error,
    )


def _iso(moment: datetime) -> str:
    """UTC, `Z`-suffixed, second precision — the same spelling the producers use."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
