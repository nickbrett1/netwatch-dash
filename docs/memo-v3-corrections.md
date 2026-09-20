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

---

## 7. Correction (2026-09-20, later): CI **can** build and run a macOS payload

v2 §10.2 claimed the assembled payload *"cannot be executed in CI (macOS binaries
on a Linux runner), so the first real run of it is on the host."* **That is
wrong as stated**, and the price it described does not have to be paid.

**The agent is already native macOS.** Checked against the Buildkite API:

```
name:        mac-studio-1 / mac-studio-2
user_agent:  buildkite-agent/4.0.3.14354 (darwin; arm64)
os_id:       osx_26.6.2
meta_data:   queue=mac-studio-linux
```

It runs natively from Homebrew (`/opt/homebrew/bin/buildkite-agent`, as `nick`).
The queue is only *named* `mac-studio-linux`, and the queue's own description
says what it is: **"Mac Studio self-hosted Docker agent (native arm64,
OrbStack)"** — a native macOS host that runs *steps* inside Linux containers.

That is the entire reason a macOS binary cannot execute: every step in
`.buildkite/pipeline.yml` carries

```yaml
plugins:
  - docker#v5.13.0:
      image: "python:3.13-slim"
      platform: linux/arm64
```

**Drop the docker plugin from a step and it runs natively on arm64 macOS.** No
new agent, no queue to provision, no metered fleet (the cluster's `macos-*`
queues are Buildkite's hosted product, which the `buildkite` capability exists
to avoid — and they are not the machine we deploy to).

**§8.4 still stands, on its own terms.** It correctly dissolved the *build
matrix* problem (no targets ⇒ one build unit ⇒ no macOS runner needed for
cross-target builds). It was never a claim that macOS execution is impossible.
The two findings are independent, and only §10.2 was wrong.

### What improves

- **The fragile half of §10.2 disappears.** `pip install --platform
  macosx_11_0_arm64 --only-binary=:all:` exists only because the build ran on
  Linux. On a native arm64 macOS step a normal install resolves arm64 *macOS*
  wheels, with no cross-install to get wrong.
- **The payload gets a real gate.** Unpack `release/netwatch-dash-any.tar.gz`,
  run `bin/netwatch-dash --version`, start it, curl `/healthz` — on the exact
  artifact, before it is published. This is the tangible form of D1: producer
  and consumer exercised together, on the platform that runs them.

### What must be handled, not hand-waved

1. **A native step is not sandboxed.** Its `HOME` is `/Users/nick`, which holds
   `~/netwatch/gateway_rtt.csv`, `~/.local/state/netwatch/.rtt_streak` and
   `~/.config/netwatch/config` — live alerting state. The containerised steps are
   insulated from this; a native one is not. The smoke test must therefore run
   with a temp `HOME` and the app's own overrides (`NETWATCH_DASH_STATE`,
   `NETWATCH_DASH_GWCSV`, `NETWATCH_DASH_CONFIG`) pointed at fixtures, on a
   random loopback port so it cannot collide with the real service on 8791.
   Precondition, not follow-up.
2. **`.buildkite/pipeline.yml` is genproj-owned and rewritten on regeneration.**
   There is no knob for a second, differently-provisioned step: `buildkite.queue`
   is a single string for the whole pipeline, and `github-release.targets` (a)
   controls only the build matrix and (b) is refused for a non-rust language.
   So the durable route is an app-owned script issuing `buildkite-agent pipeline
   upload` with the macOS step — `scripts/` is seeded once and never overwritten.
   Hand-editing works but dies at the next regeneration.

### `any` clarified (this was right, and worth stating exactly)

v2 §10.2's claim that the single artifact is keyed `any` is **confirmed in the
generator source** (`src/generator/target-labels.js`):

```
export const UNIVERSAL_TARGET = "any";
```

> an artifact that is not architecture-specific (a Node bundle, a pure-python
> `.pyz`) publishes under this key rather than claiming a triple

with `targetCandidates()` appending it **last** for every host, and
`validateFetchLaunch` stating: *"A node or python project is allowed: its release
publishes one architecture-independent asset under the universal key, which the
launcher's candidate list already falls back to."*

The distinction that matters: **`any` is not a target you declare.** Putting it
in `github-release.targets` fails the config enum; declaring any real target on a
Python project fails `validateReleaseTargets` (*"A '{language}' project's output
is architecture independent, so it ships as one asset under the universal key
instead"*). Omitting `targets` is what yields the `any`-keyed asset. Python gets
`any` by **default**, not by declaration.
