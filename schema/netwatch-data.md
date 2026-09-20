# netwatch data contract

The contract between the **producers** (`producers/`, run on `mac-studio` under
launchd) and this **consumer** (the dashboard). Both sides live in this repo so
they are versioned and tested together; this file is the piece CI asserts
against.

> **Captured 2026-09-20.** All samples in `tests/fixtures/` are **real bytes**
> from `mac-studio`, not transcribed.

## Where the data lives (all read-only)

| Path | Writer | Reader |
| --- | --- | --- |
| `~/.local/state/netwatch/events.jsonl` | `netwatch probe` / `netwatch speed` | dashboard |
| `~/netwatch/gateway_rtt.csv` | `gwping.py` | dashboard |
| `~/.local/state/netwatch/events.jsonl.archive-*` | log rotation | dashboard (optional) |
| `~/.local/state/netwatch/.rtt_streak` | `netwatch` | dashboard |
| `~/.local/state/netwatch/.last_alert` | `netwatch` | dashboard |
| `~/.config/netwatch/config` | human | dashboard (**key whitelist only**) |
| `~/netwatch/orbi.env` | human | **never** |

## 1. Two clocks — the one rule that must not be broken

`events.jsonl` timestamps are **UTC** (`…Z`). `gateway_rtt.csv`'s `ts_iso` is
**local (EDT)**, while its `unixtime` column is the true UTC epoch
(confirmed at capture: `ts_iso=12:59:46` ↔ `unixtime=1789664386` = 16:59:46Z).

**Canonical internal time is `unixtime`.** Join `events.jsonl` by parsing the
`Z`, and convert to local only at render. Joining on `ts_iso` misaligns every
chart by four hours and looks plausible while doing it.

## 2. `events.jsonl` — one JSON object per line

### `kind: "probe"`
Base fields (present since at least 2026-09-13):

```json
{"ts":"<ISO-UTC Z>","kind":"probe","gw":"192.168.1.1","iface":"en0","rtt_ms":2.409,"loss_pct":0.0,"media":"1000baseT full-duplex flow-control energ"}
```

**Fields added on/around 2026-09-20** (satellite/peer check — present in the
2026-09-20 fixture, absent from the 2026-09-13 one):

| Field | Meaning |
| --- | --- |
| `link` | `"wired"` / … |
| `rx_mbps` | interface RX at probe time (float) |
| `saturated` | bool; if true the producer **suppresses** latency/loss alarms |
| `peer` | wired peer address (`NET_PEER`) |
| `peer_ms` | peer RTT |
| `peer_loss_pct` | peer loss |

> **This is the drift that motivates co-location.** The memo (`§2.1`, written
> 2026-09-19) documented only the base fields; `netwatch` changed on
> **2026-09-20 02:16** and added six more. The consumer must treat absent
> fields as `unknown`, never as zero, and must **count** unrecognised fields.

`media` is **not a fixed format**. Seen: `1000baseT full-duplex flow-control
energ…` and `autoselect`. **Unknown is never 1000baseT.**

### `kind: "speed"`
```json
{"ts":"<ISO-UTC Z>","kind":"speed","dl_mbps":523.4,"ul_mbps":565.6,"ping_ms":4.9,"server":"Verizon New York, NY"}
```

### Rotation
`events.jsonl.archive-<ts>` files exist — history resets at rotation. Read the
glob or accept the reset; ~1,900 lines ≈ 7 days at capture.

## 3. `gateway_rtt.csv`

Header: `ts_iso,unixtime,target,rtt_ms`. Empty `rtt_ms` = loss. `target` ∈
`{gw, wire, wl, net}`. Marker rows are `# <type> <ISO-ts> <rest>`:

```
# start 2026-…  pid=<n> gw=<ip> net=<ip>
# stop <ISOTS>
# loss <ISOTS> gw
# burst_start <ISOTS> 163.9ms
# burst_end <ISOTS> peak=163.9ms
# link_change <ISOTS> autoselect (1000baseT <full-duplex,…>) -> ?
# orbi <ISOTS> {"internet":0,"internet_head":"STATUS","internet_text":"GOOD","satellites_num":3,"devices_num":28}
# iferrs <ISOTS> {"iface":"en0","ierrs":0,"oerrs":0,"coll":0,"rx_bytes":93364822283,"tx_bytes":21108729333}
```

- Growth ≈ **8.4 MB/day** (24 MB at capture, ~3 GB/yr). **Never parse per
  request.**
- `wc -l` counts marker rows too — do not use it as a sample count.
- **Rotation/truncation is not handled by a persisted offset yet.** A byte
  offset is only valid while the file grows. The reader must invalidate it on
  inode change or `size < offset` (this is an open gap, not a solved one).
- **How the dashboard reads it (2026-09-20).** The seed is the *whole file*, not
  a bounded tail: the file is the producer's own rotated record, so the retention
  window is what bounds the rollup, and a fixed-size seed silently shortened how
  far back the panel could see. Retention is **three days** (4,320 minutes × 4
  targets), and the drill-in reports the newest six hours per minute and folds
  everything older into **hourly** rows — each row carries `bucket_s`, so a
  quiet minute and a quiet hour do not read the same. Per minute over three days
  would be ~4,300 points per target, which is more ink than screen.

### Drift measured against the live log

The consumer was run over the **entire live log** (mac-studio,
`~/netwatch/gateway_rtt.csv`, 2026-09-20): **510,698 lines parsed** — 501,425
samples, 9,272 markers, 1 header, **0 bad lines**. The first run reported
`drift.count == 2`, and both were `# stop` — a marker the writer has always
emitted but this schema had never listed. It is now listed. A clean run is the
invariant to hold: any non-zero drift against real data means producer and
schema have separated, and the number is surfaced in `/healthz`.

## 4. Small state files

- `.rtt_streak` — single integer, consecutive over-threshold probes. Read as
  the canary's progress bar (`2` at capture).
- `.last_alert` — `kind epoch` lines, in the order `loss`, `rtt`, `dl`, `ul`.

## 5. Config (`~/.config/netwatch/config`) — whitelist only

Keys present at capture (2026-09-20, 11 keys):

```
ALERT_COOLDOWN  DL_WARN_MBPS  NET_PEER  NOTIFY_MACOS  NTFY_TOPIC  RTT_ALERT
RTT_WARN_CONSEC  RTT_WARN_MS  SAT_LOAD  SAT_MBPS  UL_WARN_MBPS
```

The dashboard reads **only** `RTT_WARN_MS`, `RTT_EXCESS_MS`, `RTT_WARN_CONSEC`,
`DL_WARN_MBPS`, `UL_WARN_MBPS`, `ALERT_COOLDOWN`, `NET_PEER` and `RTT_ALERT`.
**`NTFY_TOPIC` is never read and must never appear in any response.** A test
asserts the whitelist cannot surface it.

`RTT_EXCESS_MS` is the dashboard's own key, agreed 2026-09-20 (§7): the
dashboard reads it out of the producer's config file rather than managing a
second one, and the producer ignores it. It is a judgement about a WAN, so the
host sets it and the dashboard invents no default — an absent key is reported as
absent (schema §6) and simply skips that comparison.

`RTT_ALERT` arrived on 2026-09-20 with the decision to stop treating gateway
ICMP as a health signal (§7); the producer defaults it to `off`, so it logs RTT
to `events.jsonl` but raises no `rtt`/`rtt-local` alarm. Reading it lets
`/healthz` state that plainly. `SAT_MBPS`/`SAT_LOAD` are **not** read yet — they
explain the producer's `saturated` suppression and become worth reading when the
link panel exists, not before.

## 6. Compatibility rule (the point of one repo)

Because producers and consumer are released together:

1. CI runs the consumer's parser against `tests/fixtures/` — real producer bytes.
2. The parser **counts** unrecognised fields and markers and exposes the count
   on `/healthz` and the page. A silent schema change becomes a visible number.
3. Unknown is `unknown` — never coerced to a good value, never to zero.

---

## 7. Health signal model (revised 2026-09-20)

**Gateway ICMP RTT is demoted from health signal to diagnostic.** Measured
2026-09-20: the Orbi's own echo reply floor is ~1.75 ms spiking to 76 ms, while
traffic *through* it to 1.1.1.1 / 8.8.8.8 runs 3–7 ms avg with 0 % loss, en0 is
clean (`Ierrs=Oerrs=Coll=0` across ~93 GB in), and the link is idle (~40 kbps).
The delay is the consumer router's deprioritised control-plane CPU path, not the
wire, not the host, not load. `>4.0 ms x3` on that signal fires on a healthy
network (confirmed: an `rtt` alert fired 2026-09-20T12:54Z).

| Signal | Source | Cadence | Role |
| --- | --- | --- | --- |
| **Packet loss** | probe `loss_pct`; empty `rtt_ms` in the CSV | 300 s / 1–5 s | **Health** — trusted across every path |
| **Forwarded-path RTT** | CSV `net` (1.1.1.1), `wire` | 5 s / 1 s | **Health** — replaces gw as the latency canary; judged on its excess over `gw`, §7.1 |
| **Link rate** | probe `media` + `# link_change` | 300 s | **Health** — renegotiation / bad-cable canary |
| **en0 error counters** | *not recorded yet — see §8* | — | **Health (missing)** — the direct hardware answer |
| **Throughput** | `kind:speed`; optional Mac↔peer iperf3 | daily | **Health** — capacity; the recommended gw-ICMP replacement |
| **Peer RTT** | probe `peer_ms`, `peer_loss_pct` | 300 s | Health, with the caveat that the peer can itself be busy |
| **Saturation** | probe `rx_mbps` vs `SAT_MBPS`, `saturated` | 300 s | Context; suppresses latency alarms on a busy link |
| **Router state** | `# orbi` marker | irregular | Context: `internet_text`, satellites, devices |
| **Gateway RTT** | CSV `gw`, probe `rtt_ms` | 1 s / 300 s | **Diagnostic only — never enters status or alerting** |

### Status derivation (revises memo v2 §3.2)

`crit` if loss > 0 on any probe **or** link ≠ 1000baseT **or** forwarded-path RTT
(`net`/`wire`) **at fault** (§7.1) **or** probe stale **or** en0 errors > 0.
`warn` if the forwarded path is at fault but not `crit`, `rtt_streak > 0`, or
throughput below threshold. **Gateway RTT is not an input.** It is rendered as a
labelled diagnostic ("router CPU") so the localisation panel still attributes
*where* a fault is — but a `gw` spike with `wire` flat now reads as **benign
router control plane**, not "fault at the router LAN port". That inversion is a
change to the panel legend, not just a threshold.

### 7.1 What "forwarded path at fault" means (2026-09-20)

The forwarded path is `gateway + transit`, so the gateway's own echo-reply
latency is *inside* the number. Judging it absolutely therefore judges the
router as much as the WAN, and the size of that mistake is measurable: on
mac-studio the forwarded path never went below **5.42 ms** in 120 consecutive
minutes, while `RTT_WARN_MS=4.0` was the threshold — so the rule fired on
**120 of 120** minutes and the verdict said the same thing forever. The doc
above had already identified the cause ("the Orbi's own echo reply floor is
~1.75 ms spiking to 76 ms") and then set the ceiling *below* the path's floor.

Two conditions now, and their names are what the panel reports
(`reading.fault`):

| Condition | Test | Names |
| --- | --- | --- |
| `beyond-router` | `net − gw > RTT_EXCESS_MS` | the WAN leg — the fault §7's prose describes |
| `path-ceiling` | `net > RTT_WARN_MS` | the whole path, router included |

- The **residual** `net − gw` cancels the router out, so its floor is ~0 ms on a
  healthy WAN whatever the router is doing. It is the primary signal.
- `RTT_WARN_MS` keeps its literal meaning — a ceiling on the forwarded path —
  and is a **backstop** for a path that is slow everywhere, not only beyond the
  router. It must clear the path's own floor to be a threshold at all.
- Both samples must come from the **same source**: the residual subtracts the
  *csv's* gateway (1 s cadence) from the csv's forwarded path, never the probe's
  300 s `rtt_ms`, which is minutes of router jitter away.
- A missing threshold skips that comparison; it is never a comparison against
  zero. A missing gateway skips the residual and leaves the ceiling.
- **A gateway spike can no longer raise the status by construction**: it only
  *shrinks* the residual. §7's invariant is preserved by mechanism rather than
  by ignoring the input.

Calibration on mac-studio, 120 minutes (2026-09-20): residual p50 3.4 ms, p95
7.1 ms, worst 81 ms. Host values `RTT_EXCESS_MS=10`, `RTT_WARN_MS=25` give a
**4 % dwell (5/120)** — three genuine beyond-router minutes and two where the
router was itself slow — against the old rule's 100 %. Both numbers are above
every normal minute measured and below every real event.

The identical test lives in `parse.path_fault` and is used by both
`derive_status` and the panel's `reading`, so the status and the sentence
explaining it cannot disagree.

### Sequencing consequence

The primary latency signal (forwarded path) lives **only in the CSV**, so it
needs the tailer + rollup (**Phase 3**). `events.jsonl` (Phase 2, cheap) still
yields **loss, media, peer_ms, saturated** — a real tile without path latency,
but not a path-latency trend. Either accept that, or pull the rollup forward.

## 8. Producer addition required: interface error counters

Nothing records en0 `Ierrs/Oerrs/Coll` today, so "is the hardware failing?" is a
hand-run snapshot in the dashboard's only trend. Spec:

- **Where:** `gwping.py`, on the existing `# orbi` cadence (60s), as its own
  `# iferrs` marker line in the CSV. Same writer, same file, no new daemon.
- **Shape:** `# iferrs <ISO-ts> {"iface":"en0","ierrs":0,"oerrs":0,"coll":0,"rx_bytes":…,"tx_bytes":…}`
- **Rules:** the producer writes **raw, since-boot** counters and nothing else —
  the dashboard owns delta derivation. A decrease between samples (interface
  reset, reboot, counter wrap) is a **reset and reads as 0 new errors**, never
  as a negative rate; the first sample after a restart establishes a baseline
  and also reads as 0 rather than inventing history it cannot see. Field set is
  additive; unrecognised keys are counted as drift (`iferrs.<key>`), never
  dropped. Reading the counters can legitimately fail (no such interface) and
  the producer then omits the marker rather than writing a partial one.
- **Status:** shipped 2026-09-20. Producer in `producers/gwping.py`
  (`iface_counters`), consumer in `src/netwatch_dash/parse.py` (`parse_iferrs`,
  `iface_error_deltas`), fixture `tests/fixtures/iferrs_sample.csv` (real bytes
  from mac-studio en0), tests in `tests/test_parse.py`.
- **Why it matters:** it is the one signal that directly answers "failing
  hardware" rather than inferring it, and the network analysis could only
  provide it as a point-in-time reading.
