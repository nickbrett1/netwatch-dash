# The dashboard app

A read-only view of the netwatch data files, served by FastAPI. It computes no
alerts and sends none (memo v3 D5); alerting stays `netwatch`'s job. It is a
**LaunchAgent**, not a LaunchDaemon, bound to the tailnet address only, with no
authentication (memo v3 §3) — so the bind address is the whole of the access
control and the code never becomes a way to reach something the tailnet cannot.

```
bin/netwatch-dash            exec python/bin/python3.13 -m netwatch_dash
python -m netwatch_dash      serve (no flags) · --version · --help
```

## Layout

| File | What it owns |
| --- | --- |
| `src/netwatch_dash/__main__.py` | the entry point: `--version`/`--help` *before* the ASGI import, then `serve()` |
| `src/netwatch_dash/settings.py` | every path and address, from the environment, with host defaults |
| `src/netwatch_dash/buildinfo.py` | reads `build-info.json` back out of the payload; compares shipped producers with installed ones |
| `src/netwatch_dash/app.py` | the HTTP surface |
| `src/netwatch_dash/snapshot.py` | one read of the live state, and the status that comes out of it |
| `src/netwatch_dash/events.py` | the bounded tail-reader for `events.jsonl` |
| `src/netwatch_dash/parse.py` | the tolerant parsers (the data contract, `schema/netwatch-data.md`) |

`--version` and `--help` deliberately do not import the ASGI stack: the Buildkite
smoke gate probes them against the assembled payload on the macOS agent, and a
gate that fails because of an import a *different* machine lacks is a gate failing
for a reason that is not the payload's. The gate then starts the payload for real
and reads `/healthz` back, so "the flags answer" and "the app serves" are two
separate verdicts rather than one check standing in for the other
(`docs/release-payload.md`, "At smoke time").

## Configuration

Nothing is required and nothing is compiled in. `deploy/install-host.sh` writes
`$HOME/.config/netwatch-dash/env`; the launchd job inherits it.

| Variable | Default |
| --- | --- |
| `NETWATCH_DASH_BIND` | `127.0.0.1:8791` — loopback, **not** `0.0.0.0`. The installer writes the tailnet address (`100.77.144.14:8791`); the default has to be the one that cannot be reached from off-host, because there is no auth to fall back on |
| `NETWATCH_DASH_STATE` | `~/.local/state/netwatch` |
| `NETWATCH_DASH_GWCSV` | `~/netwatch/gateway_rtt.csv` |
| `NETWATCH_DASH_CONFIG` | `~/.config/netwatch/config` |
| `NETWATCH_DASH_TZ` | `America/New_York` — render only; the canonical internal time is `unixtime` (schema §1) |
| `NETWATCH_DASH_BUILD_INFO` | discovered (see below) |
| `NETWATCH_DASH_HOME` | `$HOME` — the root the installed producers are looked for under |

A bind that is not `host:port` raises at start rather than at request time: a
service that cannot listen should say so once, not answer 500 to every request.

### Finding `build-info.json`

`scripts/build-payload.sh` writes it to `<payload-root>/share/netwatch-dash/`.
The bundled interpreter is installed at `<payload-root>/python`, so `sys.prefix`'s
parent is the payload root; a source checkout is found relative to the module.
Both are tried, because the same code runs in both places. An explicit
`NETWATCH_DASH_BUILD_INFO` wins and is used **even if it does not exist** —
naming a file means the answer should be about that file, not about whichever one
was discovered instead.

## `GET /healthz`

Always `200`, always JSON, never raises. It is a `siteMonitor` target, so a
failure here would be a false alarm about the Mac rather than a fact about the
dashboard; a file it cannot read is a *reported absence*.

```json
{
  "status": "warn",
  "status_reason": ["RTT_ALERT=off on the host: …", "not read yet - …"],
  "version": "0.1.15", "commit": "deadbeef…",
  "build": {"found": true, "path": "…", "tag": "v0.1.15", "target": "aarch64-apple-darwin",
            "built_at": "…", "python": {…}, "wheel": {…}, "dependencies": {…}},
  "producers": {"shipped": {…}, "installed": {…}, "skew": [], "missing": ["netwatch"]},
  "launcher": {"found": true, "path": "…/fetch-launch.sh", "version": "0.1.18", "sha256": "…"},
  "config": {"RTT_ALERT": "off", "RTT_WARN_MS": "4.0"},
  "config_error": null,
  "data": {"drift_count": 0, "drift": {"unknown_fields": [], "bad_lines": 0, …}},
  "sources": {"events": {…}, "config": {…}, "csv": {"read": false, "reason": "…"}},
  "not_implemented": {"/api/localise": "needs the csv targets …"}
}
```

Five things it says, and why each is shaped the way it is:

- **`version`/`commit` come from `build-info.json`, never from a constant.** A
  constant is true in exactly one of the two places this code runs — a wheel
  unpacked on a host, or a checkout in CI — and a lie in the other. A payload
  that cannot read its own build says so (`build.found: false`, with the reason).
- **`producers.skew` and `producers.missing` are counted, and kept apart.** Skew
  is a host running a *different* producer; missing is a host running *none* of
  it. Both are normal on a machine the producers do not run on (a CI agent, a
  laptop), so neither is an error — the count is the signal (schema §6). Today
  `gwping.py` is expected to be in `skew`: the repo's copy already carries the
  `# iferrs` marker and the host's does not (producers/README.md).
- **`launcher` is what started this payload, read back from the three variables
  `fetch-launch.sh` exports on the way out.** A payload started by hand (a test,
  `python -m netwatch_dash`) reports `found: false` rather than a guess. The
  values are the launcher's own belief about itself — its version and digest can
  lag this payload by one start, because a swap takes effect next start — so they
  are reported, never corrected here.
- **`config` is the whitelist, so `NTFY_TOPIC` cannot appear.** `parse_config`
  drops it by not listing it; a test asserts the token cannot reach a response
  even when it is in the file (schema §5). Verified against the host's own config
  file, not only a fixture.
- **`data.drift_count` is the parser's count over the real log.** Zero across the
  full 2012-line `events.jsonl` as captured (2026-09-20), which is the invariant
  schema §6 asks CI to hold: a non-zero count means producer and schema have
  separated, and it is a number rather than a missing panel.
- **`status`** is `unknown` only when there is nothing to derive it from (no
  usable probe); otherwise it is the health signals. Every reason is printed, and
  every `ok` is qualified by what was *not* looked at — an unqualified green light
  is the failure this list exists to prevent.

## The data endpoints

| Endpoint | Reads | Phase |
| --- | --- | --- |
| `/api/summary` | everything below, as one object for the tile — status, its reasons, the inputs it used, the newest probe and speed test, the state files, drift and sources | **2 — built** |
| `/api/probe` | the most recent `kind: "probe"` events (oldest first), `?limit=` 1–1000 | **2 — built** |
| `/api/speed` | the most recent `kind: "speed"` events | **2 — built** |
| `/api/link` | `media` history and the `# link_change` markers — renegotiation / bad-cable | 3 |
| `/api/localise` | which segment is at fault: the csv targets (`gw`/`wire`/`wl`/`net`) and `# iferrs` deltas | 3 |
| `/api/incidents` | the `# loss` / `# burst_*` markers, `.last_alert`, `.rtt_streak` | 3 |

`/healthz` names the ones that do not exist yet, with the reason, so a missing
endpoint is a documented absence rather than a 404 to interpret.

`/api/probe` and `/api/speed` both return `returned`, `in_window` and
`truncated`. That trio is not decoration: *"no data"* and *"no data in the window
I read"* are different answers, and a page that cannot tell them apart will draw
an empty chart over a full file.

### Rules the readers are built to

- **The CSV is never parsed per request** (schema §3) — ~8.4 MB/day, ~3 GB/yr. It
  is read by a tailer into a per-minute rollup, and the endpoints read the
  rollup. A byte offset is only valid while the file grows, so the reader
  invalidates it on inode change or `size < offset` (memo v3 §2, item 8 — an open
  gap, specified but not implemented).
- **`events.jsonl` is read as a bounded tail** (`events.py`, 1 MiB default).
  Cheap enough per request, but not unbounded, and the bounded read *also*
  sidesteps the rotation gap: there is no persisted offset to invalidate, so a
  rotated or truncated file is a shorter tail rather than a pointer into a
  different file. `bytes_read`, `lines` and `truncated` are in every answer.
- **Record order is not trusted.** The real file is append-ordered; the captured
  fixture is not, and a reader that trusted position would answer with whatever
  line happened to be last. Everything is ordered by the record's own `ts`, and a
  record whose `ts` cannot be parsed is dropped *and counted* — it cannot be
  placed in time, so it cannot be used.
- **A torn tail is not drift.** The writer appends to a file a reader may open
  mid-write, so a final line that does not parse is discarded silently. A final
  line that *does* parse is a record and is subject to the contract like any
  other — otherwise the last line of every file would be exempt.
- **`unknown` is never coerced, in either direction.** A missing field is `null`,
  an unrecognised field/marker is counted into the drift number (schema §6), and
  an absent `media` is *unknown*, not a bad link — the mirror error to reading
  unknown as good.

### Two judgement calls, recorded

- **`link_ok` is three-valued.** `media` saying `1000bas` is `true`; `media`
  present and saying anything else (including `autoselect`) is `false` and moves
  the status to `crit`, because that is the renegotiation canary (schema §7);
  `media` absent is `null` — unknown, and it moves nothing.
- **`.rtt_streak` is reported and never passed to the status.** The streak counts
  consecutive over-threshold *gateway* probes, and gateway RTT is not a health
  input (schema §7); the producer's own `RTT_ALERT=off` default says the same.
  It is rendered with its provenance, like the gateway RTT beside it. Throughput
  *is* a status input (§7's table) — but only a current measurement: a days-old
  speed test is not evidence about the link now, so a stale one is ignored rather
  than reported as either good or bad.
