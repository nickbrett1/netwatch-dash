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

## Interpreter policy (open, see schema doc)

`gwping.py`'s shebang is `#!/usr/bin/env python3` (Homebrew 3.14.7 if run by
hand) but its launchd job invokes `/usr/bin/python3` (the Xcode CLT shim). One
script, two interpreters depending on how it starts. Pin this explicitly when
`deploy/install-host.sh` exists rather than inheriting both.
