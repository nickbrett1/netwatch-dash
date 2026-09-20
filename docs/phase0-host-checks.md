# Phase 0 checks — evidence

Run 2026-09-20 against the real host and the real NAS. Every line here was
observed, not assumed. Anything unchecked is marked **open**.

## 1. Release token — ✅ satisfied

`doppler secrets --only-names -p common -c prd` on mac-studio lists
`GITHUB_RELEASE_TOKEN`. The release step resolves it from the same
config, so builds 2–4 passing is explained rather than lucky.

## 2. Where the dashboard is reachable from — ✅ tailnet IP, not MagicDNS

| Question | Answer | How it was checked |
| --- | --- | --- |
| Is the port free on mac-studio? | **Yes** — `8791` has no listener (`8788` is taken by something else) | `lsof -nP -iTCP:8791`, `netstat -an -p tcp` |
| Can the NAS reach mac-studio on the tailnet? | **Yes** — `100.77.144.14:22` connects from the NAS host | `/dev/tcp` connect test |
| Can the NAS *resolve* `mac-studio`? | **No** — `gaierror: Name or service not known` | `python3 socket.gethostbyname`, `resolv.conf` = `nameserver 192.168.1.1` only |

So every machine-facing URL uses the tailnet IP
`http://100.77.144.14:8791`. There is no MagicDNS name to fall back on: trust
the IP, not the name.

`homepage` runs `network_mode: host`, so the checks above are exactly what its
server-side fetch will see — the container has no network namespace of its own.

## 3. Homepage version and widget schema — ✅ v2.4.0

`ghcr.io/gethomepage/homepage:latest`, image label **v2.4.0**, up 2 days
(healthy). Config at `/volumeUSB1/usbshare/docker/homepage/config`.

Two widget shapes are already in use on this NAS, and both are v2.4.0-correct:

```yaml
# server-side fetch (must be reachable from the NAS, so: tailnet IP)
widget:
    type: customapi
    url: http://127.0.0.1:8797/?days=7&limit=10
    refreshInterval: 30000
    display: dynamic-list
    mappings:
        items: data.ranking
        name: name
        label: count
        format: number

# browser-side fetch (must be reachable from the *viewer*)
widgets:
  - type: iframe
    src: http://nas:8790/
    classes: h-[30rem] md:h-[32rem] lg:h-[34rem]
    refreshInterval: 600000
```

Note the asymmetry the existing config already relies on: `deepseek-balance`
pairs `siteMonitor: http://127.0.0.1:8790/health` (server, localhost) with
`href: http://nas:8790/history` (browser, LAN name). The netwatch tile needs
neither form of localhost — it is on another machine — so `customapi`/`siteMonitor`
use the tailnet IP and `href` uses the tailnet IP too, which works from tailnet
clients and fails from LAN-only ones. **The tile therefore lives in a tailnet-only
view**, which is a real constraint, not a detail.

`format: percent` expects a **0–1 fraction**, not 0–100 (source: v2.4.0 docs).
If a widget shows loss, divide by 100 in the API payload, not in the config.

## 4. Producer plists match the versioned copies — ✅ byte-identical

`diff` of each `deploy/plists/*.plist` (after `$HOME` templating) against the
installed `~/Library/LaunchAgents/*.plist`:

```
IDENTICAL  com.nick.netwatch.gwping
IDENTICAL  com.nickbrett.netwatch.probe
IDENTICAL  com.nickbrett.netwatch.speed
```

`launchctl list` shows all three loaded; `gwping` running (pid 11045), the two
interval jobs last exited 0. `deploy/install-host.sh --dry-run` reports all three
as already up to date and creates nothing — the dry run is side-effect free
(verified: `~/.local/share/netwatch-dash` and `~/.config/netwatch-dash` did not
exist afterwards).

## Open

- Dashboard is not built yet, so `http://100.77.144.14:8791/healthz` has never
  actually answered a `siteMonitor` poll. The *reachability* is proven; the
  *response* is not.
- `deploy/install-host.sh --dry-run` has been run against mac-studio; the real
  (non-dry) install has **not**. Nothing in launchd has been changed.
- `fetch-launch.sh` is not present at `~/.local/share/netwatch-dash/`, so the
  dashboard job would be written but could not start. Cold-start bootstrap is
  still a manual step, as the installer warns.
