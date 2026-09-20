# netwatch-dash — design memo **v3** (corrections to v2)

**Created:** 2026-09-20 · **Status: CORRECTIONS.** This supersedes the stale parts of
`memos/CcpxBB2kKdLY7wNYueQEm5` (v2). Everything below was verified on 2026-09-20
against the repo, the Buildkite org (`nick-brett`), and both hosts (mac-studio, NAS) —
not restated from the earlier memos.

Read v2 for the design; read this for what changed and what to do about it.

---

## 1. Decisions taken since v2

| # | Decision | Why |
|---|---|---|
| **D1** | **The producers live in this repo and ship in the same release as the dashboard.** | With one release and one CI run, the pair is tested together and the version skew that a separate deploy would allow cannot exist. The release is also the only *versioned* artifact, so this is the only way "which producer is this host running?" becomes answerable. |
| **D2** | **Host wiring is version-controlled** — `deploy/plists/` holds all five launchd jobs (3 existing, 2 new). | The existing jobs were invisible, hand-rolled and on one machine; the installer can now reconcile them. |
| **D3** | **Nothing in launchd is decommissioned.** | All three scheduled jobs are data producers *and* alerters. The unscheduled scripts (`check_link.sh`, `flapwatch`, the two probes) are the ones "replaced" — and they were dormant, so removing them changes nothing operationally. Alerting stays netwatch's job. |
| **D4** | **The contract is a first-class artifact** — `schema/netwatch-data.md` + `tests/fixtures/`. | Co-location alone does not catch schema drift; a tested contract does (see §3, finding 5). |
| **D5** | **Alerting is unchanged.** ntfy + macOS notifications stay in netwatch. | The dashboard computes no alerts and sends none. Restated because "decommission the checks" was raised; it would have removed alerting. |

## 2. v2 claims that are now wrong

| # | v2 said | Reality (2026-09-20) | Action |
|---|---|---|---|
| 1 | §8.1 / §12.13 / §14.6: no `doppler`; the release step reads `GH_TOKEN` from the fleet env; "regenerate with `doppler` before Phase 1" | **Already resolved.** The repo was regenerated to 12 capabilities *after* v2 was written (`0b7183a`); `doppler` is in and the release step resolves `GITHUB_RELEASE_TOKEN` from **`common/prd`** at run time. Build 1 (pre-Doppler) failed with `GH_TOKEN is not set on the agent`; builds 2–4 passed. | Delete the recommendation; §14.6 is moot. Just confirm the secret exists in `common/prd` and `DOPPLER_TOKEN` is on the agent (build 2 implies both). |
| 2 | §7: homepage is v2.1.2 | **v2.4.0** | Re-verify `customapi`/`iframe` mappings and the `format: percent` caveat against 2.4.0 before writing the YAML. |
| 3 | §7: both widgets use `http://mac-studio:8791` | The NAS resolver is `192.168.1.1` with `TS_ACCEPT_DNS=false`, so **`mac-studio` → NXDOMAIN from the NAS**. `100.77.144.14:8791` routes fine (direct, ~2.4 ms). | Use the **tailnet IP** in both widget URLs, or add MagicDNS to the NAS resolver. This breaks the `customapi` widget on day one otherwise. |
| 4 | §7: "same split as `deepseek-balance`" | `deepseek-balance` uses `iframe` only (+ `container`/`siteMonitor`); `customapi` is a *different* tile (`Top MCP Tools (7d)`). | The plan is fine; fix the citation. Add `siteMonitor: http://100.77.144.14:8791/healthz` so the tile shows red when the Mac is down. |
| 5 | §2.1: probe records are `{ts, kind, gw, iface, rtt_ms, loss_pct, media}` | **Six fields were added on 2026-09-20** (netwatch mtime 02:16, i.e. after v2): `link`, `rx_mbps`, `saturated`, `peer`, `peer_ms`, `peer_loss_pct` — a peer/satellite check, matching the new `NET_PEER`/`SAT_MBPS`/`SAT_LOAD` config keys. `saturated` **suppresses** latency/loss alarms. | Parser treats absent fields as `unknown` and **counts** unrecognised ones. Captured in `schema/netwatch-data.md` §2. This drift is the concrete argument for D1. |
| 6 | §2.3(b): `speed.log`/`probe.log` are leftovers the script never writes | The **plists** set `StandardOutPath`/`StandardErrorPath` to them, so launchd writes them. The *structured* speed result is still in `events.jsonl` only. | Correct the reasoning; the conclusion (design against `events.jsonl`) stands. |
| 7 | §4 / App. D: "mac-studio has no Docker runtime" | **OrbStack 29.4.0 is present** (arm64). No Watchtower, and `fetch-launch` still declares `conflicts: [docker-container]`. | "No container" is still correct; the *reason* is wrong. Fix it so it isn't re-litigated. |
| 8 | §5: tolerates growth and a torn tail | Does **not** handle shrink / rotation / inode change. A persisted byte offset then points into a different file. | Reader must invalidate the offset on inode change or `size < offset`. Open gap — specified, not yet implemented. |
| 9 | §14.1: the name `netwatch-dash` is fine | A **tailnet node** `netwatch-dash` (100.90.186.109) already exists — this repo's own devcontainer agent. | Give the service the `mac-studio` identity; never address it as `netwatch-dash`. |
| 10 | §9: the launchd plists are new work | The three **existing** producer plists are now versioned (`deploy/plists/`) and hardcode `/Users/nick/...`. | `install-host.sh` must template the home path, else "version-controlled" ≠ "reproducible". |
| 11 | — | `gwping.py`'s shebang is `#!/usr/bin/env python3` (Homebrew 3.14.7) while its plist runs `/usr/bin/python3` (Xcode CLT shim). One script, two interpreters. | Pin the interpreter explicitly when the installer exists. |

## 3. What still stands from v2 (unchanged — and still the highest-value items)

- **§2.3a two clocks** — re-confirmed at capture (`ts_iso` 12:59:46 EDT ↔ `unixtime` = 16:59:46Z). Still the single most important detail.
- **§5** tail-read + per-minute rollup; **never** parse the CSV per request (~8.4 MB/day).
- **§5.6** tolerate everything, never throw, never 500.
- **§3.2** never coerce `unknown` to `ok`; **§12.6** unknown `media` is not 1000baseT.
- **§6** API surface; **§9** LaunchAgent (user) layout — **auto-login is confirmed enabled**, so a LaunchAgent is viable headless.
- **§10.2** bundling rationale — `/usr/bin/python3` is confirmed to be the Xcode shim.
- **§11** no auth, tailnet-only bind, key whitelist, never open `orbi.env`, never emit `NTFY_TOPIC`.

## 4. Release shape (refines v2 §10.3)

One payload carries both producers and the dashboard; CI tests the pair. But:

- Producers **install to stable paths** (`~/netwatch/`, `~/.local/bin/`) and are never exec'd from `current/` — so a bad payload cannot take alerting down (fetch-launch already fails open).
- **Co-release, co-adopt** — one version, one click, no skew. Manual activation was rejected because it reintroduces exactly the skew D1 removes.
- `/healthz` reports the dashboard version, the **installed producer version**, and the **drift count**, all traceable to one manifest `sha256`.

## 5. Revised phase plan

- **Phase 0 (no code):** (a) confirm `GITHUB_RELEASE_TOKEN` in `common/prd`; (b) switch the NAS widget URLs to the tailnet IP (or fix the resolver); (c) re-verify the homepage 2.4.0 widget schema.
- **Phase 1:** the tile — parser + fixtures + drift counter + `/healthz`(version) + the `release-artifacts.sh` bundle (§10.2) + `install-host.sh` with templated plists.
- **Phases 2–4:** as v2.

## 6. Open decisions

1. **Producer/consumer integration-test depth.** Fixture round-trip (now) → producer-emits / parser-parses in CI (Phase 1) → a **shared schema module** imported by both (follow-up; implies the producers run in the bundled Python env, which is a behaviour change to the alerting path and must be decided deliberately).
2. **Orbi panel privacy** (v2 §14.4) — surface `internet_text`/satellites/devices, or omit. Still open.
