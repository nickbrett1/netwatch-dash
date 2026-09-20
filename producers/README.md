# Producers (netwatch) — provenance

These are **verbatim copies** of the scripts that *produce* the data this
dashboard reads. They were pulled from `mac-studio` on **2026-09-20**. Until
that date they existed **only on that one machine**, with no version control
anywhere (`git rev-parse` fails in `~/netwatch` and `~/.local`) and no
installer referencing them.

Nothing here is modified. Do not treat this directory as the deployment path —
see `deploy/` and `schema/netwatch-data.md`.

| File here | Origin on mac-studio | Role |
| --- | --- | --- |
| `gwping.py` | `~/netwatch/gwping.py` | Continuous writer of `~/netwatch/gateway_rtt.csv`; also the authenticated Orbi poll that emits `# orbi` markers and `# link_change` |
| `netwatch` | `~/.local/bin/netwatch` | The `probe` (300 s) and `speed` (daily 04:00) subcommands; appends to `~/.local/state/netwatch/events.jsonl` |
| `flapwatch` | `~/.local/bin/flapwatch` | Not scheduled by any launchd job |
| `check_link.sh` | `~/netwatch/check_link.sh` | Not scheduled by any launchd job |
| `probe_icmp_vs_tcp.py` | `~/netwatch/probe_icmp_vs_tcp.py` | Diagnostic |
| `probe_lan_tcp.py` | `~/netwatch/probe_lan_tcp.py` | Diagnostic |

## Not included (deliberately)

- **`~/netwatch/orbi.env`** — router credentials, mode 0600. `gwping.py` reads it
  via `CREDS = ~/netwatch/orbi.env`; no credential is inlined in the source
  (checked). The dashboard never opens this file, and neither does this repo.
- **`~/.config/netwatch/config`** — contains `NTFY_TOPIC` (a bearer-like
  capability token). Only the *key names* are captured, in
  `tests/fixtures/config_keys.txt`.
- `~/netwatch/gateway_rtt.csv` and `~/.local/state/netwatch/*` — runtime state,
  not source. Sampled into `tests/fixtures/`.

## Drift log — why these copies are already stale

The copies were taken 2026-09-20 and were byte-identical to the host at that
moment (`shasum` verified). Within the same day the host had moved on twice
more. This is the case for co-location, recorded as it happens:

| When (host mtime) | What changed | Consumer impact |
| --- | --- | --- |
| 2026-09-20 02:16 | `netwatch` gained `link`, `rx_mbps`, `saturated`, `peer`, `peer_ms`, `peer_loss_pct`, and `saturated` now suppresses latency alarms | Consumer parsing the v2 field set would silently ignore six fields and could alarm on a link the producer considers busy |
| 2026-09-20 09:13 | `netwatch` gained `RTT_ALERT` (default `off`) and stopped raising `rtt`/`rtt-local` alarms; reporter flag changed `DEGRADED` -> `LOSS` | Contract §5 was missing a config key; §7's decision is now enforced in the producer too |
| (this repo) | `gwping.py` gained `# iferrs` per §8 | Consumer must know the marker or it counts as drift forever |

`producers/netwatch` was refreshed from the host after the second change; its
sha256 is now `d728858a…`. `producers/gwping.py` deliberately differs from the
host: it carries the `# iferrs` addition from schema §8, which is not deployed
yet.

**Rule that follows from this:** a copy here is evidence of what the producer
*wrote at capture time*, never a claim about what is running *now*. Any
comparison against the host that is more than a day old is assumed wrong until
re-verified by `shasum`.


`gwping.py`'s shebang is `#!/usr/bin/env python3` (Homebrew 3.14.7 if run by
hand) but its launchd job invokes `/usr/bin/python3` (the Xcode CLT shim). One
script, two interpreters depending on how it starts. Pin this explicitly when
`deploy/install-host.sh` exists rather than inheriting both.
