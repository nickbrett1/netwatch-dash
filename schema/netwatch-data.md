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
# loss <ISOTS> gw
# burst_start <ISOTS> 163.9ms
# burst_end <ISOTS> peak=163.9ms
# link_change <ISOTS> autoselect (1000baseT <full-duplex,…>) -> ?
# orbi <ISOTS> {"internet":0,"internet_head":"STATUS","internet_text":"GOOD","satellites_num":3,"devices_num":28}
```

- Growth ≈ **8.4 MB/day** (24 MB at capture, ~3 GB/yr). **Never parse per
  request.**
- `wc -l` counts marker rows too — do not use it as a sample count.
- **Rotation/truncation is not handled by a persisted offset yet.** A byte
  offset is only valid while the file grows. The reader must invalidate it on
  inode change or `size < offset` (this is an open gap, not a solved one).

## 4. Small state files

- `.rtt_streak` — single integer, consecutive over-threshold probes. Read as
  the canary's progress bar (`2` at capture).
- `.last_alert` — `kind epoch` lines, in the order `loss`, `rtt`, `dl`, `ul`.

## 5. Config (`~/.config/netwatch/config`) — whitelist only

Keys present at capture:

```
ALERT_COOLDOWN  DL_WARN_MBPS  NET_PEER  NOTIFY_MACOS  NTFY_TOPIC
RTT_WARN_CONSEC  RTT_WARN_MS  SAT_LOAD  SAT_MBPS  UL_WARN_MBPS
```

The dashboard reads **only** `RTT_WARN_MS`, `RTT_WARN_CONSEC`, `DL_WARN_MBPS`,
`UL_WARN_MBPS`, `ALERT_COOLDOWN` (and optionally `NET_PEER`). **`NTFY_TOPIC` is
never read and must never appear in any response.** A test asserts the summary
and link payloads contain no `NTFY`.

## 6. Compatibility rule (the point of one repo)

Because producers and consumer are released together:

1. CI runs the consumer's parser against `tests/fixtures/` — real producer bytes.
2. The parser **counts** unrecognised fields and markers and exposes the count
   on `/healthz` and the page. A silent schema change becomes a visible number.
3. Unknown is `unknown` — never coerced to a good value, never to zero.
