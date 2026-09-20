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
| `src/netwatch_dash/parse.py` | the tolerant parsers (the data contract, `schema/netwatch-data.md`) |

`--version` and `--help` deliberately do not import the ASGI stack: the Buildkite
smoke gate runs them against the assembled payload, and a gate that fails because
of an import a *different* machine lacks is a gate failing for a reason that is
not the payload's.

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
  "status": "unknown",
  "status_reason": ["not read yet — loss_pct, media/link and probe age: …"],
  "version": "0.1.15", "commit": "deadbeef…",
  "build": {"found": true, "path": "…", "tag": "v0.1.15", "target": "aarch64-apple-darwin",
            "built_at": "…", "python": {…}, "wheel": {…}, "dependencies": {…}},
  "producers": {"shipped": {…}, "installed": {…}, "skew": [], "missing": ["netwatch"]},
  "config": {"RTT_ALERT": "off", "RTT_WARN_MS": "4.0"},
  "config_error": null,
  "data": {"drift_count": null, "reason": "no reader implemented yet"},
  "not_implemented": ["/api/summary", "…"]
}
```

Four things it says, and why each is shaped the way it is:

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
- **`config` is the whitelist, so `NTFY_TOPIC` cannot appear.** `parse_config`
  drops it by not listing it; a test asserts the token cannot reach a response
  even when it is in the file (schema §5).
- **`status` is `unknown`, and says why.** Every input to the status comes from a
  reader that does not exist yet, and `derive_status` answers `unknown` for
  inputs it does not have (schema §6, §7). Gateway RTT is never an input, by
  design: it measures the router's control-plane CPU, not the path, so a `gw`
  spike beside a flat `wire` is benign, not a fault.

`data.drift_count` is `null` rather than `0` for the same reason: `0` would claim
a clean parse that has not happened.

## The endpoints that read data

Not built yet; they arrive with the readers, and each names what it is for.

| Endpoint | Reads | Phase |
| --- | --- | --- |
| `/api/summary` | everything below, as one object for the tile | 2 |
| `/api/probe` | recent `kind: "probe"` events — `loss_pct`, `media`/`link`, `peer_ms`, `saturated`, probe age | 2 |
| `/api/link` | `media` history and `# link_change` markers — renegotiation / bad-cable | 2 |
| `/api/speed` | `kind: "speed"` events — capacity, the recommended gw-ICMP replacement | 2 |
| `/api/localise` | which segment is at fault: `csv` targets (`gw`/`wire`/`wl`/`net`) + `# iferrs` deltas | 3 |
| `/api/incidents` | `# loss` / `# burst_*` markers, `.last_alert`, `.rtt_streak` | 3 |

Two constraints are fixed before any of them is written:

- **The CSV is never parsed per request** (schema §3) — ~8.4 MB/day, ~3 GB/yr. It
  is read by a tailer into a per-minute rollup, and the endpoints read the
  rollup. A byte offset is only valid while the file grows, so the reader
  invalidates it on inode change or `size < offset` (memo v3 §2, item 8 — an open
  gap, specified but not implemented).
- **`unknown` is never coerced.** A missing field, an unrecognised marker and an
  unknown `media` value are reported as unknown, and unrecognised fields/markers
  are *counted* into the drift number this endpoint surfaces (schema §6).
