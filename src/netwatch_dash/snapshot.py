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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import csvrollup, events
from .parse import (
    derive_status,
    media_is_gigabit,
    parse_config,
    parse_last_alert,
    parse_streak,
    speed_is_stale,
)
from .settings import Settings

# The contract's signals that are deliberately *not* status inputs, named in
# every answer so their absence reads as a decision rather than an oversight.
NOT_STATUS_INPUTS = (
    (
        "peer RTT and peer loss (probe `peer_ms`/`peer_loss_pct`): §7 lists them as "
        "health signals but leaves them out of the derivation, because a busy peer "
        "is not a path fault. Reported in /api/localise as evidence instead."
    ),
)

# Endpoints that exist by name and not by behaviour, and why. Empty now: the
# csv rollup was the last thing owed.
NOT_IMPLEMENTED: dict[str, str] = {}

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
    csv: csvrollup.Rollup

    # --- the csv rollup, as status inputs --------------------------------

    @property
    def csv_age_s(self) -> float | None:
        """How long ago the csv last had a sample with a timestamp."""
        last = self.csv.last_unixtime
        if last is None:
            return None
        return max(0.0, self.now.timestamp() - last)

    @property
    def csv_current(self) -> bool | None:
        """Whether the csv reading speaks for the link *now*.

        None before anything has been read — the seed runs off the request path,
        so "not read yet" and "read and stale" have to stay distinguishable.
        """
        age = self.csv_age_s
        if age is None:
            return None
        return age <= self.settings.csv_stale_s

    @property
    def forwarded_rtt(self) -> tuple[str, dict] | None:
        if self.csv_current is not True:
            return None
        return self.csv.forwarded_latest()

    @property
    def forwarded_rtt_ms(self) -> float | None:
        found = self.forwarded_rtt
        return found[1]["rtt_ms"] if found else None

    @property
    def csv_loss_pct(self) -> float | None:
        """The worst per-minute loss in the newest minute the csv holds.

        A minute at the 1 s cadence is ~60 samples, so one dropped ICMP reads as
        ~1.7% — measured across 500 real minutes on mac-studio, 99.4% of minutes
        have none, and the lossy ones are real events (worst: 10%). That is why
        this can be a `crit` input at face value: it is not a twitchy signal, and
        the producer's own `# loss` markers are the same events in event form.
        """
        if self.csv_current is not True or self.csv.last_unixtime is None:
            return None
        minute = int(self.csv.last_unixtime // 60)
        buckets = self.csv.minutes.get(minute)
        if not buckets:
            return None
        return max((b.as_dict()["loss_pct"] or 0.0) for b in buckets.values())

    @property
    def en0_errors(self) -> int | None:
        """New error counters since the window began (§8), or None if unread.

        A decrease is a reset and reads as 0, and the first marker is a baseline
        rather than a delta — so this can only ever say "these many new errors
        were observed", never invent history.
        """
        if not self.csv.iferrs:
            return None
        totals = self.csv.error_totals
        return totals["ierrs"] + totals["oerrs"] + totals["coll"]


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
            # The csv's loss, from the per-minute rollup. Both are loss; kept
            # apart so a red tile can say which side saw it.
            "csv_loss_pct": self.csv_loss_pct,
            "link_ok": link_ok(probe),
            # §7: the forwarded path (`net` 1.1.1.1, then the wired peer) is the
            # latency canary that replaced the gateway. Its threshold is the
            # host's own RTT_WARN_MS, and a host that has not set one gets no
            # comparison rather than a comparison against zero.
            "forwarded_rtt_ms": self.forwarded_rtt_ms,
            "forwarded_warn_ms": _config_number(self.config, "RTT_WARN_MS"),
            "probe_age_s": self.probe_age_s,
            "probe_stale_s": self.settings.probe_stale_s,
            # New en0 errors observed in the csv window (§8).
            "en0_errors": self.en0_errors,
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
        self._csv_reasons(reasons)

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

        reasons.extend(f"not read yet - {item}" for item in NOT_STATUS_INPUTS)
        return reasons

    def _csv_reasons(self, reasons: list[str]) -> None:
        """What the csv did, and what that means for the status.

        Every branch here exists because the alternative is a status that looks
        informed when it is not: an unread file, a stale reading and a reading
        with no threshold are three different silences.
        """
        if self.csv.error:
            reasons.append(f"gateway_rtt.csv: {self.csv.error}")
        if not self.csv.seeded:
            reasons.append(
                "gateway_rtt.csv is still being read for the first time "
                "(the seed runs off the request path), so the forwarded path, "
                "csv loss and en0 counters are not inputs yet"
            )
            return
        if self.csv.reading:
            reasons.append("a gateway_rtt.csv refresh is in flight; this answer is at most one refresh old")
        age = self.csv_age_s
        if age is None:
            reasons.append(
                "gateway_rtt.csv: no sample with a usable `unixtime` in the window"
            )
        elif self.csv_current is False:
            reasons.append(
                f"the newest gateway_rtt.csv sample is {age:.0f}s old "
                f"(stale after {self.settings.csv_stale_s:.0f}s), so it does not "
                "speak for the link now"
            )
        else:
            forwarded = self.forwarded_rtt
            if forwarded:
                target, sample = forwarded
                threshold = _config_number(self.config, "RTT_WARN_MS")
                if threshold is not None and sample["rtt_ms"] is not None and sample["rtt_ms"] > threshold:
                    reasons.append(
                        f"forwarded-path RTT ({target}) is {sample['rtt_ms']:.1f}ms, "
                        f"above the host's RTT_WARN_MS={threshold:g}"
                    )
                elif threshold is None:
                    reasons.append(
                        "the host sets no RTT_WARN_MS, so the forwarded-path RTT is "
                        "reported but has no threshold to be judged against"
                    )
            loss = self.csv_loss_pct
            if loss:
                reasons.append(f"loss {loss:.1f}% in the newest csv minute")
        errors = self.en0_errors
        if errors:
            reasons.append(f"{errors} new interface error(s) in the csv window")

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
            "csv": self.csv.window(),
        }

    # --- the panels --------------------------------------------------------

    def link(self) -> dict:
        """`/api/link`: the link rate now, and every renegotiation in the window.

        The probe's `media` is the current answer; `# link_change` is the history.
        They are different ages of the same question, so both are returned with
        their own timestamps rather than merged into one confident line.
        """
        probe = self.probe
        changes = self.csv.markers_of("link_change", "start", "stop")
        return {
            "current": {
                "media": probe.get("media") if probe else None,
                "link_ok": link_ok(probe),
                "iface": probe.get("iface") if probe else None,
                "ts": probe.get("ts") if probe else None,
                "age_s": self.probe_age_s,
                "reading": (
                    "the newest probe records no `media`: link rate unknown"
                    if link_ok(probe) is None
                    else "gigabit"
                    if link_ok(probe)
                    else "not gigabit - the renegotiation canary"
                ),
            },
            "changes": [m.as_dict() for m in changes][-50:],
            "change_count": len(changes),
            "window": self.csv.window(),
            "drift": self.events.drift.as_dict(),
            "sources": self.sources(),
        }

    def localise(self) -> dict:
        """`/api/localise`: which hop is slow, from the csv's per-target rollup.

        §7's inversion lives here: a `gw` spike with `net`/`wire` flat is the
        router's control-plane CPU, which is benign for the path — so the answer
        carries a `reading` that names the hop the numbers actually implicate,
        with the numbers it used.
        """
        threshold = _config_number(self.config, "RTT_WARN_MS")
        series = {t: self.csv.minute_series(t, 120) for t in csvrollup.TARGETS}
        latest = {t: self.csv.latest_of(t) for t in csvrollup.TARGETS}
        return {
            "thresholds": {"rtt_warn_ms": threshold},
            "targets": {
                t: {"latest": latest[t], "minutes": series[t]} for t in csvrollup.TARGETS
            },
            "host": {
                "iface": self.csv.iface(),
                "errors": self.csv.error_totals,
                "intervals": self.csv.error_intervals[:50],
            },
            "peer": self._peer(),
            "reading": self._reading(threshold, latest),
            "window": self.csv.window(),
            "drift": self.events.drift.as_dict(),
            "sources": self.sources(),
        }

    def _peer(self) -> dict | None:
        """The peer check from the newest probe — evidence, not a status input."""
        probe = self.probe
        if not probe or "peer" not in probe:
            return None
        return {
            "peer": probe.get("peer"),
            "peer_ms": _number(probe.get("peer_ms")),
            "peer_loss_pct": _number(probe.get("peer_loss_pct")),
            "age_s": self.probe_age_s,
        }

    def _reading(self, threshold: float | None, latest: dict) -> dict:
        """The localisation verdict, and the numbers behind it.

        Deliberately a comparison of the forwarded path against the gateway, not
        a threshold test on one of them: the question "is this hop at fault?" is
        only answerable relative to the path.
        """
        def rtt(target: str) -> float | None:
            sample = latest.get(target)
            return sample["rtt_ms"] if sample else None

        forwarded = self.forwarded_rtt_ms
        gateway = rtt("gw")
        if forwarded is None or gateway is None:
            return {
                "verdict": "unknown",
                "detail": "the csv window does not hold both a gateway and a "
                "forwarded-path sample, so no hop can be implicated",
                "numbers": {"gw": gateway, "forwarded": forwarded, "threshold": threshold},
            }
        numbers = {"gw": gateway, "forwarded": forwarded, "threshold": threshold}
        if threshold is None:
            return {
                "verdict": "unknown",
                "detail": "the host sets no RTT_WARN_MS, so there is no threshold "
                "to judge either hop against",
                "numbers": numbers,
            }
        if forwarded > threshold:
            return {
                "verdict": "forwarded path",
                "detail": f"the forwarded path is {forwarded:.1f}ms, above RTT_WARN_MS"
                f"={threshold:g}; the delay is beyond the router, so it is a path or"
                " upstream fault",
                "numbers": numbers,
            }
        if gateway > threshold:
            return {
                "verdict": "router control plane",
                "detail": f"the gateway replies in {gateway:.1f}ms while the forwarded"
                f" path is {forwarded:.1f}ms (both against RTT_WARN_MS={threshold:g}):"
                " the router is deprioritising its own ICMP, which §7 measured as"
                " benign for the path",
                "numbers": numbers,
            }
        return {
            "verdict": "no hop implicated",
            "detail": f"gateway {gateway:.1f}ms and forwarded path {forwarded:.1f}ms are"
            f" both within RTT_WARN_MS={threshold:g}",
            "numbers": numbers,
        }

    def incidents(self) -> dict:
        """`/api/incidents`: what the producers already called an event.

        This endpoint does not decide what an incident is — it reports the
        markers the producers wrote (`# loss`, `# burst_*`) plus the alert kinds
        and timestamps `netwatch` last raised, which is the only place the
        alerting side's view is recorded.
        """
        loss = self.csv.markers_of("loss")
        bursts = self._bursts()
        return {
            "alerts": {
                kind: {"unixtime": epoch, "at": _iso_from_epoch(epoch, self.settings.tz)}
                for kind, epoch in sorted(self.last_alert.items())
            },
            "alerts_error": self.last_alert_error,
            "last_alert": self._last_alert(),
            "loss": [m.as_dict() for m in loss][-50:],
            "bursts": bursts[:50],
            "counts": {"loss": len(loss), "bursts": len(bursts)},
            "window": self.csv.window(),
            "drift": self.events.drift.as_dict(),
            "sources": self.sources(),
        }

    def _last_alert(self) -> dict | None:
        """The newest alert the producer raised, with its age."""
        if not self.last_alert:
            return None
        kind, epoch = max(self.last_alert.items(), key=lambda kv: kv[1])
        return {
            "kind": kind,
            "unixtime": epoch,
            "at": _iso_from_epoch(epoch, self.settings.tz),
            "age_s": max(0.0, self.now.timestamp() - epoch),
        }

    def _bursts(self) -> list[dict]:
        """`# burst_start`/`# burst_end` paired in file order.

        A `burst_start` with no `burst_end` is reported as an open burst rather
        than dropped: the writer was interrupted mid-burst, and that is itself
        worth seeing.
        """
        out: list[dict] = []
        open_burst: dict | None = None
        for marker in self.csv.markers_of("burst_start", "burst_end"):
            if marker.kind == "burst_start":
                if open_burst is not None:
                    out.append(open_burst)
                open_burst = {
                    "start": marker.as_dict(),
                    "end": None,
                    "open": True,
                }
            elif open_burst is not None:
                open_burst["end"] = marker.as_dict()
                open_burst["open"] = False
                out.append(open_burst)
                open_burst = None
        if open_burst is not None:
            out.append(open_burst)
        return list(reversed(out))

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
            # The csv, as the status saw it: the same two numbers that went into
            # the derivation, with the window they came out of.
            "csv": {
                **self.csv.window(),
                "age_s": self.csv_age_s,
                "current": self.csv_current,
                "forwarded": (
                    {"target": self.forwarded_rtt[0], **self.forwarded_rtt[1]}
                    if self.forwarded_rtt
                    else None
                ),
                "loss_pct": self.csv_loss_pct,
                "en0_errors": self.en0_errors,
                "iferrs": len(self.csv.iferrs),
            },
            "not_status_inputs": list(NOT_STATUS_INPUTS),
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


def build(
    settings: Settings,
    now: datetime | None = None,
    rollup: csvrollup.Rollup | None = None,
) -> Snapshot:
    """One read of everything the dashboard can reach.

    `rollup` is passed in rather than read here: the csv is a tailer with a
    cursor and a seed that runs off the request path, so its life is longer than
    a request's (`csvrollup.RollupCache`). A snapshot without one says so — it
    does not read the file, which is what "never parsed per request" means.
    """
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
        csv=rollup if rollup is not None else csvrollup.Rollup(path=settings.csv_path),
    )


def build_with_csv(
    settings: Settings, cache: csvrollup.RollupCache, now: datetime | None = None
) -> Snapshot:
    """`build`, with the rollup taken from the cache (refreshed if due).

    The snapshot's own `now` is the cache's clock too: a caller that pins the
    clock (a test, or a replay) must get an answer whose freshness and retention
    are judged at that instant, not at whatever the wall clock says. Otherwise a
    pinned call would still refresh on real-now and prune the very window the
    caller asked about.
    """
    epoch = now.timestamp() if now is not None else None
    return build(settings, now, cache.get(settings, now_epoch=epoch))


def _iso_from_epoch(epoch: float, tz: str) -> str:
    """An epoch in the host's own zone — the spelling the producers use.

    §1: the epoch is canonical, and local time is a rendering. Markers and
    `.last_alert` are *written* in local time, so reading them back in it is what
    makes them comparable with the file they came from.
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return (
        datetime.fromtimestamp(float(epoch), tz=zone)
        .replace(microsecond=0)
        .isoformat()
    )


def _iso(moment: datetime) -> str:
    """UTC, `Z`-suffixed, second precision — the same spelling the producers use."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
