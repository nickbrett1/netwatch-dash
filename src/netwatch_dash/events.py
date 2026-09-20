"""The `events.jsonl` reader.

Cheap enough to read per request, unlike the CSV (schema §3) — but not unbounded.
The file grows ~288 probe lines a day and never shrinks until it is rotated, so
the reader reads a bounded **tail** and reports how much it read rather than
pretending it saw the whole history. A bounded tail also sidesteps the gap the
CSV reader has to solve: there is no persisted offset to invalidate, so a rotated
or truncated file is simply a shorter tail, never a pointer into a different file.

Two tolerances that are load-bearing:

* **Order is not assumed.** The real file is append-ordered, but the captured
  fixture is not, and a reader that trusted position would answer with whatever
  line happened to be last. Everything here is ordered by the record's own
  timestamp, and a record whose timestamp cannot be parsed is dropped and
  **counted** — it cannot be placed in time, so it cannot be used, and a silent
  disappearance would look like a clean file.
* **A torn tail is not drift.** The writer appends to a file a reader may open
  mid-write, so the final unterminated line is parsed tolerantly and never
  counted. A half-written record is the writer being busy, not the schema having
  changed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .parse import Drift, parse_event

# ~5000 probe lines: weeks of 300 s probes, and a bounded read on every request.
DEFAULT_TAIL_BYTES = 1 << 20


def timestamp(record: dict) -> datetime | None:
    """A record's `ts` as an aware UTC datetime, or None.

    `events.jsonl` timestamps are UTC `…Z` (schema §1). The fixture's own
    capture confirmed the other file's `ts_iso` is *local*, which is why this
    reads `ts` and never a rendered time: joining on local time misaligns
    everything by the UTC offset and looks plausible while doing it.
    """
    value = record.get("ts")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class EventLog:
    """What the bounded read of `events.jsonl` found."""

    path: Path
    # Ordered by `ts`, oldest first, so `[-1]` is the newest. A record without a
    # usable timestamp is not in here — it is in `drift.bad_lines`.
    probes: list[dict] = field(default_factory=list)
    speeds: list[dict] = field(default_factory=list)
    drift: Drift = field(default_factory=Drift)
    lines: int = 0
    size: int | None = None
    bytes_read: int = 0
    # True when the read started mid-file: the answer is about a window, not the
    # whole history, and says so rather than implying completeness.
    truncated: bool = False
    error: str | None = None

    @property
    def newest_probe(self) -> dict | None:
        return self.probes[-1] if self.probes else None

    @property
    def newest_speed(self) -> dict | None:
        return self.speeds[-1] if self.speeds else None


def read(path: Path, tail_bytes: int = DEFAULT_TAIL_BYTES) -> EventLog:
    """Read the tail of an `events.jsonl`. Never raises."""
    log = EventLog(path=path)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - tail_bytes)
            log.truncated = start > 0
            fh.seek(start)
            data = fh.read()
    except OSError as exc:
        log.error = f"{type(exc).__name__}: {exc}"
        return log

    log.size = size
    log.bytes_read = len(data)
    if log.truncated:
        # The read started mid-line unless the window happens to begin exactly on
        # a boundary, so the first fragment is dropped rather than parsed.
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1 :]

    text = data.decode("utf-8", errors="replace")
    raw_lines = text.split("\n")
    torn_tail = bool(text) and not text.endswith("\n")
    last = len(raw_lines) - 1

    for index, line in enumerate(raw_lines):
        if not line.strip():
            continue
        log.lines += 1
        # The final line of an unterminated file may be mid-append. Only a line
        # that does NOT parse is treated as torn: a complete record that merely
        # lacks its trailing newline is a record, and its drift has to be counted
        # or the last line of every file would be exempt from the contract.
        torn = torn_tail and index == last
        if torn and parse_event(line, None) is None:
            continue
        record = parse_event(line, log.drift)
        if record is None:
            continue
        kind = record.get("kind")
        if kind not in ("probe", "speed"):
            continue
        if timestamp(record) is None:
            log.drift.bad_lines += 1
            continue
        (log.probes if kind == "probe" else log.speeds).append(record)

    log.probes.sort(key=timestamp)
    log.speeds.sort(key=timestamp)
    return log
