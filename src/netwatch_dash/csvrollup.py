"""The `gateway_rtt.csv` tailer and per-minute rollup.

Contract: `schema/netwatch-data.md` §1 (two clocks), §3 (the file), §7 (what it
is worth) and §8 (the `# iferrs` counters).

The file is ~8.4 MB/day and ~3 GB/yr and **must never be parsed per request**
(§3). Two things make that possible, and both are choices worth naming.

**There is no offset file.** §3 lists the rotation gap as open: a byte offset is
only valid while the file grows, so the reader "must invalidate it on inode
change or `size < offset`". The answer here is that there is no persisted offset
to invalidate. This process is read-only by charter — it is the dashboard's whole
premise — so a cursor file would be the first thing it ever wrote, and it would
then need flushing, locking and invalidation of its own on top of the file it
describes. The cursor lives in memory for the life of the process instead, and
the invalidation rule is the same one a persisted cursor would need: the file is
no longer the one the cursor was following (different inode, or shorter than the
offset), so the cursor is dropped and the tail is re-seeded. A restart therefore
costs one *bounded* read rather than one full read.

That choice has a cost, and it is reported rather than hidden: the rollup covers
the window it actually read, and every answer carries `window` — `bytes`,
`first_unixtime`, `last_unixtime`, `truncated` — so "no link change in the
window" can never be mistaken for "no link change".

**The first read is off the request path.** Seeding reads up to
`csv_tail_bytes` (4 MiB ≈ 12 h at the measured rate); incremental reads after
that are whatever the producer wrote since, which is a few kB. `RollupCache`
runs the seed on a thread at start-up (`prewarm`) and serves the previous
rollup while a refresh is in flight, so no request ever waits on the file.

Per §1 the canonical time is `unixtime`. Samples carry it directly. Markers carry
a *local* ISO timestamp and no zone, so they are converted through the configured
tz (`NETWATCH_DASH_TZ`) — and when that fails the marker keeps its raw text with
`unixtime: null`, because an unplaceable marker is unknown, not "now".
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .parse import Drift, iface_error_deltas, parse_csv_line, parse_iferrs

# The seed window: 4 MiB ≈ 12 h at the measured 8.4 MB/day. Bounded on purpose,
# so a file that has grown to gigabytes still costs the same to start.
DEFAULT_INITIAL_TAIL_BYTES = 4 << 20
# Minutes kept. 24 h is 1,440 buckets × 4 targets, which is nothing, and it is
# the window the panels are specified over.
DEFAULT_RETENTION_S = 86400.0
# Markers kept. `# link_change` is rare; 500 is generous for the panel and bounds
# what a burst of `# orbi` lines can hold in memory.
DEFAULT_MARKER_LIMIT = 500
# How often a request may trigger an incremental read. The producer writes every
# 1-5 s; this coalesces a page's worth of requests into one read without making
# the newest sample look older than it is.
DEFAULT_REFRESH_S = 5.0
# The targets, in the order the panel reads them: the path (`net` 1.1.1.1 and the
# wired peer), then the diagnostic (`gw`).
TARGETS = ("net", "wire", "wl", "gw")
# Which targets are the forwarded path (§7): these are the latency health signal,
# and `gw` deliberately is not.
FORWARDED_TARGETS = ("net", "wire")


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class Bucket:
    """One quarter of the rollup: the samples of one target in one minute."""

    n: int = 0
    loss: int = 0
    rtt_sum: float = 0.0
    rtt_min: float | None = None
    rtt_max: float | None = None

    def add(self, rtt_ms: float | None, loss: bool) -> None:
        self.n += 1
        if loss or rtt_ms is None:
            self.loss += 1
            return
        self.rtt_sum += rtt_ms
        self.rtt_min = rtt_ms if self.rtt_min is None else min(self.rtt_min, rtt_ms)
        self.rtt_max = rtt_ms if self.rtt_max is None else max(self.rtt_max, rtt_ms)

    def as_dict(self) -> dict:
        """A minute, for the wire.

        `rtt_ms_avg` is over the samples that *have* an RTT: an empty `rtt_ms`
        is loss, and averaging it in as zero would make a minute of total loss
        look like a minute of perfect latency.
        """
        measured = self.n - self.loss
        return {
            "n": self.n,
            "measured": measured,
            "loss": self.loss,
            "loss_pct": (100.0 * self.loss / self.n) if self.n else None,
            "rtt_ms_avg": (self.rtt_sum / measured) if measured else None,
            "rtt_ms_min": self.rtt_min,
            "rtt_ms_max": self.rtt_max,
        }


@dataclass
class Marker:
    """A `# …` row, with its local timestamp converted to the canonical epoch."""

    kind: str
    raw: str
    ts_iso: str | None = None
    unixtime: float | None = None
    payload: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "ts_iso": self.ts_iso,
            "unixtime": self.unixtime,
            "raw": self.raw,
        }
        if self.payload is not None:
            out["payload"] = self.payload
        return out


@dataclass
class Cursor:
    """Where the tailer has read to. In memory, for the life of the process."""

    offset: int = 0
    size: int = 0
    inode: int | None = None
    # Bytes after the last newline. Kept rather than discarded because the
    # producer appends to a file this reads: a half-written line is normal, and
    # dropping it would drop a sample that is complete a millisecond later.
    partial: str = ""
    seeded: bool = False


@dataclass
class Rollup:
    """What the tailer managed to read, aggregated. Never raises on bad input."""

    path: Path
    minutes: dict[int, dict[str, Bucket]] = field(default_factory=dict)
    markers: list[Marker] = field(default_factory=list)
    iferrs: list[Marker] = field(default_factory=list)
    # target -> the newest sample seen for it
    latest: dict[str, dict] = field(default_factory=dict)
    drift: Drift = field(default_factory=Drift)

    # what the reader saw
    size: int | None = None
    bytes_read: int = 0
    lines: int = 0
    samples: int = 0
    window_bytes: int = 0
    truncated: bool = False
    first_unixtime: float | None = None
    last_unixtime: float | None = None
    seeded: bool = False
    restarted: str | None = None
    read_at: float | None = None
    error: str | None = None
    reading: bool = False

    # --- reading answers out of it ------------------------------------------

    def markers_of(self, *kinds: str) -> list[Marker]:
        wanted = set(kinds)
        return [m for m in self.markers if m.kind in wanted]

    def minute_series(self, target: str, limit: int) -> list[dict]:
        """The last `limit` minutes that hold this target, oldest first."""
        rows = [
            {"minute": minute, **buckets[target].as_dict()}
            for minute, buckets in sorted(self.minutes.items())
            if target in buckets
        ]
        return rows[-limit:] if limit > 0 else []

    def latest_of(self, target: str) -> dict | None:
        return self.latest.get(target)

    def forwarded_latest(self) -> tuple[str, dict] | None:
        """The newest forwarded-path sample, `net` before `wire` (§7).

        `net` is 1.1.1.1 — traffic off the LAN — so it is the one that speaks for
        the path a browser would use. `wire` is the wired peer, which can be busy
        for reasons of its own.
        """
        for target in FORWARDED_TARGETS:
            sample = self.latest.get(target)
            if sample is not None:
                return target, sample
        return None

    @property
    def error_totals(self) -> dict:
        """Counter deltas across every consecutive `# iferrs` pair (§8).

        A decrease is a reset and reads as 0 new errors, and the first marker has
        no predecessor, so it establishes a baseline and also reads as 0 — the
        dashboard never invents history it cannot see.
        """
        totals = dict.fromkeys(("ierrs", "oerrs", "coll", "rx_bytes", "tx_bytes"), 0)
        previous: dict | None = None
        for marker in self.iferrs:
            payload = marker.payload or {}
            delta = iface_error_deltas(previous, payload)
            for key, value in delta.items():
                totals[key] += value
            previous = payload
        return totals

    @property
    def error_intervals(self) -> list[dict]:
        """Each `# iferrs` pair with its own delta, newest first."""
        out: list[dict] = []
        previous: Marker | None = None
        for marker in self.iferrs:
            payload = marker.payload or {}
            out.append(
                {
                    "unixtime": marker.unixtime,
                    "ts_iso": marker.ts_iso,
                    "iface": payload.get("iface"),
                    "from": previous.unixtime if previous else None,
                    "delta": iface_error_deltas(
                        previous.payload if previous else None, payload
                    ),
                }
            )
            previous = marker
        return list(reversed(out))

    def iface(self) -> str | None:
        for marker in reversed(self.iferrs):
            iface = (marker.payload or {}).get("iface")
            if isinstance(iface, str):
                return iface
        return None

    def window(self) -> dict:
        """Exactly how much file the rollup stands on.

        The point of this object is that a panel can say "nothing in the window I
        read" instead of "nothing happened", and those are different sentences.
        """
        return {
            "path": str(self.path),
            "read": self.seeded,
            "seeded": self.seeded,
            "reading": self.reading,
            "size": self.size,
            "bytes_read": self.bytes_read,
            "window_bytes": self.window_bytes,
            "truncated": self.truncated,
            "first_unixtime": self.first_unixtime,
            "last_unixtime": self.last_unixtime,
            "minutes": len(self.minutes),
            "lines": self.lines,
            "samples": self.samples,
            "markers": len(self.markers),
            "restarted": self.restarted,
            "error": self.error,
            "read_at": self.read_at,
        }


def _to_epoch(iso: object, tz: str) -> float | None:
    """A marker's local ISO timestamp as a UTC epoch, or None.

    §1: markers are written in local time with no zone, and the canonical
    internal time is the epoch. Converting is the only honest option — and a
    value this cannot convert stays `None` rather than becoming "now", because
    the whole point of the two-clock rule is that a mis-stamped marker misaligns
    everything around it without looking wrong.
    """
    if not isinstance(iso, str) or not iso:
        return None
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.timestamp()


def _marker_from_line(line: str, tz: str, drift: Drift) -> Marker | None:
    parts = line.split(maxsplit=3)
    parsed = parse_csv_line(line, drift)
    if parsed is None or parsed[0] != "marker":
        return None
    kind = parsed[1]["type"]
    candidate = parts[2] if len(parts) > 2 else None
    epoch = _to_epoch(candidate, tz)
    marker = Marker(
        kind=kind,
        raw=line,
        ts_iso=candidate if epoch is not None else None,
        unixtime=epoch,
    )
    if kind == "iferrs":
        marker.payload = parse_iferrs(parsed[1], drift)
        if marker.payload is None:
            return None
    return marker


def _add_sample(rollup: Rollup, sample: dict) -> None:
    unixtime = _float_or_none(sample.get("unixtime"))
    target = sample.get("target")
    if unixtime is None or not isinstance(target, str):
        rollup.drift.bad_lines += 1
        return
    rollup.samples += 1
    minute = int(unixtime // 60)
    rollup.minutes.setdefault(minute, {}).setdefault(target, Bucket()).add(
        _float_or_none(sample.get("rtt_ms")), bool(sample.get("loss"))
    )
    previous = rollup.latest.get(target)
    if previous is None or unixtime >= previous["unixtime"]:
        rollup.latest[target] = {
            "unixtime": unixtime,
            "ts_iso": sample.get("ts_iso"),
            "rtt_ms": _float_or_none(sample.get("rtt_ms")),
            "loss": bool(sample.get("loss")),
            "minute": minute,
        }
    if rollup.first_unixtime is None or unixtime < rollup.first_unixtime:
        rollup.first_unixtime = unixtime
    if rollup.last_unixtime is None or unixtime > rollup.last_unixtime:
        rollup.last_unixtime = unixtime


def _consume(rollup: Rollup, lines: list[str], tz: str) -> None:
    """Feed whole lines into the rollup."""
    for line in lines:
        if not line.strip():
            continue
        rollup.lines += 1
        parsed = parse_csv_line(line, rollup.drift)
        if parsed is None:
            continue
        kind, body = parsed
        if kind == "sample":
            _add_sample(rollup, body)
        elif kind == "marker":
            marker = _marker_from_line(line, tz, rollup.drift)
            if marker is None:
                continue
            rollup.markers.append(marker)
            if marker.kind == "iferrs":
                rollup.iferrs.append(marker)


def _prune(rollup: Rollup, now_epoch: float, retention_s: float) -> None:
    """Drop minutes and markers older than the retention window.

    Markers are kept by count as well as by age: `# orbi` is irregular, and a
    bad day of them must not be able to grow the process's memory.
    """
    cutoff_minute = int((now_epoch - retention_s) // 60)
    rollup.minutes = {
        minute: buckets
        for minute, buckets in rollup.minutes.items()
        if minute >= cutoff_minute
    }
    cutoff = now_epoch - retention_s
    rollup.markers = [
        m for m in rollup.markers if m.unixtime is None or m.unixtime >= cutoff
    ][-DEFAULT_MARKER_LIMIT:]
    rollup.iferrs = rollup.iferrs[-DEFAULT_MARKER_LIMIT:]


def read(
    path: Path,
    cursor: Cursor,
    rollup: Rollup | None = None,
    *,
    tz: str = "UTC",
    now_epoch: float | None = None,
    retention_s: float = DEFAULT_RETENTION_S,
    initial_tail_bytes: int = DEFAULT_INITIAL_TAIL_BYTES,
) -> Rollup:
    """Read everything new since `cursor`, into `rollup`. Never raises.

    The three ways this can start over, all of them reported in `restarted`:

    * no cursor yet — seed from the last `initial_tail_bytes`;
    * the inode changed — the file was replaced (rotation), so the offset points
      into a different file and means nothing;
    * the file is shorter than the offset — it was truncated in place.

    The last two are §3's open gap, and the answer to it is that the seed is
    bounded and re-derivable: nothing is persisted, so nothing can be stale.
    """
    rollup = rollup if rollup is not None else Rollup(path=path)
    rollup.path = path
    rollup.error = None
    # Per-read, not sticky: "restarted" and "truncated" describe what this pass
    # did, and a value left over from the seed would make every later read claim
    # to have re-seeded.
    rollup.restarted = None
    now_epoch = time.time() if now_epoch is None else now_epoch
    bytes_read = 0

    try:
        stat = path.stat()
    except OSError as exc:
        rollup.size = None
        rollup.error = f"{type(exc).__name__}: {exc}"
        rollup.read_at = now_epoch
        return rollup

    rollup.size = stat.st_size
    reseed: str | None = None
    if not cursor.seeded:
        reseed = "first read"
    elif cursor.inode is not None and stat.st_ino != cursor.inode:
        reseed = "the file was replaced (inode changed), so the offset was dropped"
    elif stat.st_size < cursor.offset:
        reseed = "the file is shorter than the offset (truncated or rotated)"
    elif stat.st_size - cursor.offset > max(4 * initial_tail_bytes, 1):
        # A reader that has been starved (a suspended process, a stopped writer
        # that then caught up) does not replay the whole gap: the same reasoning
        # as the seed applies — take the newest bounded window and say so —
        # rather than spend seconds of a request-time refresh on data whose only
        # distinguishing feature is being old.
        reseed = "more was written than the reader could follow, so the tail was re-seeded"
    if reseed is not None:
        if cursor.seeded:
            # A re-seed is a new window: keeping the old minutes would splice two
            # files together under one timeline, which is exactly the misalignment
            # §1 exists to prevent.
            rollup = Rollup(path=path)
        cursor.offset = max(0, stat.st_size - max(0, initial_tail_bytes))
        cursor.partial = ""
        cursor.seeded = True
        rollup.restarted = reseed

    cursor.size = stat.st_size
    cursor.inode = stat.st_ino
    seek = min(cursor.offset, stat.st_size)
    carry = cursor.partial
    cursor.partial = ""
    if seek != cursor.offset:
        # The file shrank between the stat and the seek (a rotation in the
        # microsecond between the two). Drop what was carried: it belonged to the
        # file that is gone.
        carry = ""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(seek)
            skip_partial_first_line = seek > 0 and rollup.restarted is not None
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                bytes_read += len(chunk)
                carry += chunk
                # Everything up to the last newline is whole lines; what follows
                # it is a record the writer has not finished, kept for next time.
                head, sep, carry = carry.rpartition("\n")
                if not sep:
                    continue
                lines = head.split("\n")
                if skip_partial_first_line:
                    # A seeded window starts mid-file, so its first line is
                    # usually half a record. Only that one is dropped: every line
                    # after it is complete.
                    lines = lines[1:]
                    skip_partial_first_line = False
                _consume(rollup, lines, tz)
    except OSError as exc:
        rollup.error = f"{type(exc).__name__}: {exc}"
        rollup.read_at = now_epoch
        return rollup

    cursor.partial = carry
    cursor.offset = stat.st_size
    rollup.bytes_read = bytes_read
    rollup.window_bytes = stat.st_size - seek
    # The window starts at an offset into the file, so it is a window and not the
    # whole history. Said out loud so "nothing in the window" is never read as
    # "nothing happened".
    rollup.truncated = seek > 0
    rollup.seeded = True
    rollup.read_at = now_epoch
    _prune(rollup, now_epoch, retention_s)
    return rollup


class RollupCache:
    """The rollup's lifetime: seeded once, refreshed off the request path.

    A refresh takes a flag and runs outside the lock, so concurrent requests read
    the previous rollup instead of queueing behind the file. `reading` says a
    pass is in flight, which a caller can report as "this number is one refresh
    old" rather than pretending it is now.
    """

    def __init__(self, initial_tail_bytes: int = DEFAULT_INITIAL_TAIL_BYTES) -> None:
        self._lock = threading.Lock()
        self._rollup: Rollup | None = None
        self._cursor = Cursor()
        self._reading = False
        self.initial_tail_bytes = initial_tail_bytes

    @property
    def rollup(self) -> Rollup | None:
        return self._rollup

    def refresh(self, settings, now_epoch: float | None = None) -> Rollup:
        """Read incrementally, now. Blocks; callers that must not block use `get`."""
        now_epoch = time.time() if now_epoch is None else now_epoch
        with self._lock:
            if self._reading:
                # Someone else is already reading; hand back what is there. Two
                # threads must not both advance the cursor.
                return self._rollup or Rollup(path=settings.csv_path, reading=True)
            self._reading = True
            previous = self._rollup
            cursor = self._cursor
        try:
            rollup = read(
                settings.csv_path,
                cursor,
                previous,
                tz=settings.tz,
                now_epoch=now_epoch,
                retention_s=settings.csv_retention_s,
                initial_tail_bytes=self.initial_tail_bytes,
            )
        finally:
            with self._lock:
                self._reading = False
        with self._lock:
            self._rollup = rollup
        return rollup

    def get(self, settings, now_epoch: float | None = None) -> Rollup:
        """The current rollup, refreshing it if the interval has passed.

        Before the first read this returns an empty rollup flagged `reading`, not
        a block: `/healthz` is a siteMonitor target and must answer while the seed
        is still running. The seed is kicked off at start-up by `prewarm`, so in
        practice a request finds it already done.
        """
        now_epoch = time.time() if now_epoch is None else now_epoch
        current = self._rollup
        if current is None:
            if not self._reading:
                self._start_prewarm(settings, now_epoch)
            return Rollup(path=settings.csv_path, reading=True)
        if current.read_at is None or now_epoch - current.read_at >= settings.csv_refresh_s:
            return self.refresh(settings, now_epoch)
        return current

    def prewarm(self, settings, now_epoch: float | None = None) -> threading.Thread:
        """Seed the rollup in the background. Returns the thread, for tests."""
        return self._start_prewarm(settings, now_epoch)

    def _start_prewarm(
        self, settings, now_epoch: float | None = None
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self.refresh,
            args=(settings, now_epoch),
            name="csvrollup-prewarm",
            daemon=True,
        )
        thread.start()
        return thread
