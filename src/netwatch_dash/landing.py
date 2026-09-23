"""The landing page: a drill-in that shows the numbers behind the tile.

The homepage tile answers "is it OK *now*" with a verdict; this page is what
clicking it opens: the same verdict, the numbers behind it, and the daily
bandwidth runs. It reads the endpoints the app already serves (`/api/summary`,
`/api/localise`, `/api/speed`) rather than re-deriving anything.

It is a single self-contained document: inline CSS and JS, no CDN, no build step,
no template engine. The dashboard must render on a host with no network access
beyond its own tailnet, so anything it needs has to come from the app itself.

Two rules this page keeps:

* **Every number is coloured by the status rule, and explains itself.** Each
  tile takes its good/warn/crit colour from the same inputs the pill is derived
  from (schema §7), so a tile and the verdict cannot disagree; clicking a tile
  opens its definition and the thresholds actually in force, including "not set".
* **A number with nothing to judge it against is uncoloured, not green.** No
  threshold means no comparison — never a comparison against zero.

The RTT and loss charts were removed after the fact: they were built and
maintained but not read, and an unread chart is a liability, not a feature.
"""

from __future__ import annotations

LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>netwatch-dash</title>
<style>
  :root { color-scheme: dark; --line: #23262f; --dim: #9aa0aa; }
  body { margin: 0; font: 14px/1.5 -apple-system, system-ui, sans-serif;
         background: #0f1117; color: #e6e6e6; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--line); display: flex;
           gap: 14px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 15px; margin: 0; }
  .pill { padding: 2px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }
  .ok { background: #14351f; color: #5ee08a; }
  .warn { background: #3a2f10; color: #f0c14b; }
  .crit { background: #3a1518; color: #ff6b6b; }
  .unknown { background: var(--line); color: var(--dim); }
  main { padding: 20px; display: grid; gap: 18px; max-width: 1100px; }
  section { background: #161923; border: 1px solid var(--line); border-radius: 10px;
            padding: 16px; }
  h2 { margin: 0 0 12px; font-size: 12px; text-transform: uppercase;
       letter-spacing: .07em; color: var(--dim); font-weight: 600; }
  ul { margin: 0; padding-left: 18px; }
  .muted { color: #6b7280; }
  .small { font-size: 12px; }
  svg { width: 100%; display: block; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }
  .key { display: flex; gap: 6px; align-items: center; font-size: 12px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; }
  /* the "now" strip */
  .facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
           gap: 10px; }
  .fact { background: #10131b; border: 1px solid var(--line); border-radius: 8px;
          padding: 10px 12px; }
  .fact .k { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--dim); }
  .fact .v { font-size: 19px; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .fact .h { font-size: 11px; color: #6b7280; margin-top: 2px; }
  .over { color: #f0c14b; }
  .bad { color: #ff6b6b; }
  .good { color: #5ee08a; }
  .fact.link { cursor: help; }
  .fact.link:hover { border-color: #3b4152; background: #131722; }
  .fact .k::after { content: " ⓘ"; color: #4b5563; font-size: 11px; }
  #modal { position: fixed; inset: 0; background: #05070bd9; display: none;
           align-items: center; justify-content: center; padding: 20px; z-index: 10; }
  #modal.open { display: flex; }
  #modal .card { background: #161923; border: 1px solid #33394a; border-radius: 10px;
                 max-width: 560px; padding: 18px 20px; }
  #modal h3 { margin: 0 0 8px; font-size: 14px; }
  #modal p { margin: 0; color: #c3c8d2; }
  #modal .close { margin-top: 14px; font-size: 12px; color: var(--dim); }
</style>
</head>
<body>
<div id="modal"><div class="card">
  <h3 id="modal-title"></h3><p id="modal-body"></p>
  <div class="close">click anywhere, or press Esc, to close</div>
</div></div>
<header>
  <h1>netwatch-dash</h1>
  <span id="status" class="pill unknown">…</span>
</header>
<main>
  <section>
    <h2>Now</h2>
    <div class="facts" id="facts"><span class="muted small">loading…</span></div>
  </section>
  <section>
    <h2>Daily bandwidth checks (Mbps)</h2>
    <div class="legend" id="speed-legend"></div>
    <svg id="speed" viewBox="0 0 1000 240" preserveAspectRatio="none"></svg>
    <p class="muted small" id="speed-note"></p>
  </section>
</main>
<script>
// Long-form definitions, on demand. They are here rather than on the page
// because they are reference material: needed once, then noise.
const INFO = {
  forwarded: { title: "Forwarded path RTT",
    body: "ICMP round-trip time to 1.1.1.1, a public address beyond the router. " +
      "It is the only series that measures the whole path — this Mac, the LAN, " +
      "the router, the ISP and the internet — which is why it is the one that " +
      "moves the status. What counts as a fault is the part of this number the " +
      "router cannot explain: this RTT minus the gateway RTT. On a healthy link " +
      `that difference is near zero however busy the router's own ICMP is, so the ` +
      "fault is the difference reaching {rtt_excess_ms}. A fallback also fires " +
      "when the whole path, router included, is above {rtt_warn_ms} — that catches " +
      "a path slow everywhere rather than slow beyond the router. An unset " +
      "threshold means no comparison at all, never a comparison against zero." },
  gateway: { title: "Gateway RTT",
    body: "ICMP round-trip time to 192.168.1.1, the router itself. Diagnostic " +
      "only: a spike here while the forwarded path stays flat is the router's own " +
      "ICMP handling, not a fault on the path, so it never moves the status " +
      "(RTT_ALERT is off on this host)." },
  loss: { title: "Path loss",
    body: "The share of the newest probe's pings that got no reply, read from " +
      "the probe log. Loss is the one signal trusted across every path " +
      "(schema §7) — it is what survives a link that is saturated rather than " +
      "broken — so any loss at all colours this red and moves the status to " +
      "critical. The gateway's own ICMP replies are the one place a lone drop is " +
      "routine; everywhere else, expect zero." },
  link: { title: "Link rate",
    body: "The negotiated rate of the LAN interface, from the newest probe's " +
      "media and any # link_change markers. Expected is 1000baseT full-duplex. " +
      "Anything else — a renegotiation down to 100 Mbit, or half-duplex — counts " +
      "as critical, because it is usually a bad cable or a bad port and it caps " +
      "every other number on this page. No media recorded reads as unknown, " +
      "never as good." },
  down: { title: "Download",
    body: "The most recent daily speed test's download. Warn below " +
      "{dl_warn_mbps}. Throughput is a health signal (schema §7) and only a " +
      "*current* run counts: a days-old measurement is shown but never colours " +
      "the status, because it is not evidence about the link now." },
  up: { title: "Upload",
    body: "The most recent daily speed test's upload. Warn below {ul_warn_mbps}. " +
      "Same rule as download: a stale run is displayed but does not colour the " +
      "status." },
  en0: { title: "en0 errors",
    body: "Error counters for the LAN interface — input errors + output errors + " +
      "collisions, from the producer's # iferrs markers (netstat -ib). The " +
      "dashboard reports the change across the csv window, not the raw since-boot " +
      "total. Zero is the expected reading; any new error is treated as critical." },
  peer: { title: "Peer RTT",
    body: "Round-trip time to 192.168.1.2, the host at the far end of the wired " +
      "LAN link. Diagnostic: the data contract lists peer RTT and peer loss as " +
      "health signals but deliberately leaves them out of the status, because a " +
      "busy peer is not a path fault." },
};

function ns(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
function text(svg, x, y, s, attrs) {
  const t = ns("text", Object.assign({ x, y, fill: "#6b7280", "font-size": 11 }, attrs || {}));
  t.textContent = s;
  svg.appendChild(t);
  return t;
}
function mmdd(ts) { return ts ? ts.slice(5, 10) : "?"; }

// Thresholds as rendered, so the definitions can quote the host's real numbers
// instead of a number written into this file that nobody will remember to change.
let THRESHOLDS = {};

function showInfo(id) {
  const info = INFO[id];
  if (!info) return;
  document.getElementById("modal-title").textContent = info.title;
  // Substituted by hand rather than by regex: this file is a Python string, and
  // a regex here means escaping braces twice over.
  let body = info.body;
  for (const k in THRESHOLDS) body = body.split("{" + k + "}").join(THRESHOLDS[k]);
  document.getElementById("modal-body").textContent = body;
  document.getElementById("modal").classList.add("open");
}
function fmt(v, digits) {
  return v == null ? "–" : Number(v).toFixed(digits == null ? 1 : digits);
}

// ---------------------------------------------------------------- the numbers

function fact(k, v, hint, cls, metric) {
  const link = metric ? ` link" data-metric="${metric}` : "";
  return `<div class="fact${link}"><div class="k">${k}</div>
    <div class="v ${cls || ""}">${v}</div>
    ${hint ? `<div class="h">${hint}</div>` : ""}</div>`;
}

function facts(summary, localise) {
  const si = summary.status_inputs || {};
  const warn = si.forwarded_warn_ms;
  const excessLimit = si.forwarded_excess_ms;
  // The fault comes from the server (`reading.fault`), not from re-deciding the
  // rule here: two implementations of "is the path at fault" is how a tile and
  // the sentence under it end up disagreeing.
  const fault = ((localise || {}).reading || {}).fault;
  const over = fault === "beyond-router" || fault === "path-ceiling";
  const dww = si.dl_warn_mbps, uww = si.ul_warn_mbps;

  // Each colour is the status rule for that one tile, drawn from the same inputs
  // the pill is derived from (schema §7), so "why is it that colour" has one
  // answer. A tile with no threshold to judge against stays uncoloured: unknown
  // is not good, and it is not amber either.
  const pathCls = excessLimit == null && warn == null ? "" : (over ? "over" : "good");
  const lossCls = si.loss_pct == null ? "" : (si.loss_pct > 0 ? "bad" : "good");
  const linkCls = si.link_ok === true ? "good" : si.link_ok === false ? "bad" : "";
  const thrCls = (v, w) => (v == null || w == null ? "" : v < w ? "over" : "good");
  const en0Cls = si.en0_errors == null ? "" : (si.en0_errors > 0 ? "bad" : "good");

  const pathHint = fault === "beyond-router"
    ? `beyond the router by ≥ ${fmt(excessLimit, 1)} ms`
    : fault === "path-ceiling"
    ? `above ${fmt(warn, 1)} ms even for the router`
    : excessLimit == null
    ? (warn == null ? "no threshold set" : `warn ≥ ${fmt(warn, 1)} ms`)
    : `within ${fmt(excessLimit, 1)} ms of the router`;

  const tiles = [
    fact("Forwarded path RTT", fmt(si.forwarded_rtt_ms, 1) + " ms",
         pathHint, pathCls, "forwarded"),
    // Diagnostic only (schema §7): the router's own ICMP never moves the status,
    // so it is never coloured. Any brightness here is the router's CPU, not a
    // fault on the path.
    fact("Gateway RTT", fmt(si.gw_rtt_ms, 1) + " ms",
         "diagnostic — never moves the status", "", "gateway"),
    fact("Path loss", fmt(si.loss_pct, 2) + " %",
         si.loss_pct == null ? "no probe yet"
           : si.loss_pct > 0 ? "any loss is critical" : "none on the newest probe",
         lossCls, "loss"),
    fact("Link", si.link_ok == null ? "unknown" : (si.link_ok ? "up" : "down"),
         (localise.host && localise.host.iface) || "expected 1000baseT",
         linkCls, "link"),
    fact("Down", fmt(si.dl_mbps, 1) + " Mbps",
         dww == null ? "no threshold set" : `warn below ${fmt(dww, 0)}`,
         thrCls(si.dl_mbps, dww), "down"),
    fact("Up", fmt(si.ul_mbps, 1) + " Mbps",
         uww == null ? "no threshold set" : `warn below ${fmt(uww, 0)}`,
         thrCls(si.ul_mbps, uww), "up"),
    fact("en0 errors", String(si.en0_errors == null ? "–" : si.en0_errors),
         si.en0_errors == null ? "no # iferrs in window" : "ierrs+oerrs+coll; zero expected",
         en0Cls, "en0"),
    fact("Peer RTT", fmt((localise.peer || {}).peer_ms, 1) + " ms",
         (localise.peer || {}).peer || "no threshold set", "", "peer"),
  ];
  return tiles.join("");
}

// ----------------------------------------------------------------- the chart

function srv(r) { return (r.server || "").split(/[ ,]/)[0] || "?"; }

function speedChart(data, warn) {
  const svg = document.getElementById("speed");
  // `/api/speed` returns oldest-first already; drawing it in that order is what
  // makes the Sep-16 cliff read left-to-right.
  const runs = data.speeds || [];
  const note = document.getElementById("speed-note");
  const legend = document.getElementById("speed-legend");
  if (!runs.length) {
    text(svg, 10, 24, "no speed tests in the window");
    note.textContent = "the daily job has not recorded a run yet";
    return;
  }
  const maxv = Math.max(...runs.flatMap(r => [r.dl_mbps || 0, r.ul_mbps || 0])) * 1.1 || 1;
  const top = 16, base = 196;
  const slot = 936 / runs.length;
  const bw = Math.min(34, slot * 0.32);
  const py = v => base - (v / maxv) * (base - top);

  text(svg, 6, top + 4, maxv.toFixed(0));
  text(svg, 6, base + 4, "0");
  svg.appendChild(ns("line", { x1: 44, y1: base, x2: 980, y2: base, stroke: "#23262f" }));
  if (warn && warn <= maxv) {
    svg.appendChild(ns("line", { x1: 44, y1: py(warn), x2: 980, y2: py(warn),
      stroke: "#f0c14b", "stroke-width": 1, "stroke-dasharray": "4 4", opacity: 0.7 }));
    text(svg, 984, py(warn) + 4, `warn ${fmt(warn, 0)}`, { "text-anchor": "end",
      fill: "#f0c14b" });
  }
  runs.forEach((r, i) => {
    const cx = 44 + slot * i + slot / 2;
    [[-1, r.dl_mbps, "#5ee08a", "dl"], [1, r.ul_mbps, "#63b3ed", "ul"]].forEach(([side, v, col, label]) => {
      if (v == null) return;
      const x = cx + (side < 0 ? -bw - 2 : 2);
      const rect = ns("rect", { x: x.toFixed(1), y: py(v).toFixed(1), width: bw.toFixed(1),
        height: (base - py(v)).toFixed(1), fill: col, rx: 2 });
      const title = ns("title", {});
      title.textContent = `${r.ts} — ${label} ${v.toFixed(1)} Mbps, ` +
        `ping ${r.ping_ms} ms, ${r.server || "unknown server"}`;
      rect.appendChild(title);
      svg.appendChild(rect);
      text(svg, x + bw / 2, py(v) - 4, v.toFixed(0), { "text-anchor": "middle",
        fill: "#6b7280", "font-size": 10 });
    });
    text(svg, cx, base + 16, mmdd(r.ts), { "text-anchor": "middle" });
    text(svg, cx, base + 30, srv(r), { "text-anchor": "middle", "font-size": 9 });
  });
  legend.innerHTML =
    `<span class="key"><span class="swatch" style="background:#5ee08a"></span>download</span>` +
    `<span class="key"><span class="swatch" style="background:#63b3ed"></span>upload</span>`;
  const first = runs[0], last = runs[runs.length - 1];
  const drop = first.dl_mbps && last.dl_mbps
    ? (1 - last.dl_mbps / first.dl_mbps) * 100 : null;
  const servers = [...new Set(runs.map(srv))];
  note.textContent = `${runs.length} run(s), oldest ${mmdd(first.ts)}` +
    (drop != null && drop > 10
      ? ` · download is ${drop.toFixed(0)}% below the oldest run in the window ` +
        `(${fmt(first.dl_mbps, 0)} → ${fmt(last.dl_mbps, 0)} Mbps)`
      : "") +
    (servers.length > 1 ? ` · server changed: ${servers.join(" → ")}` : "");
}

// ------------------------------------------------------------------- the page

async function load() {
  const [summary, localise, speed] = await Promise.all([
    fetch("/api/summary").then(r => r.json()),
    fetch("/api/localise").then(r => r.json()),
    fetch("/api/speed").then(r => r.json()),
  ]);
  const status = document.getElementById("status");
  status.textContent = summary.status;
  status.className = "pill " + summary.status;
  const si0 = summary.status_inputs || {};
  const th = localise.thresholds || {};
  THRESHOLDS = {
    rtt_excess_ms: th.rtt_excess_ms == null ? "not set" : fmt(th.rtt_excess_ms, 0) + " ms",
    rtt_warn_ms: th.rtt_warn_ms == null ? "not set" : fmt(th.rtt_warn_ms, 0) + " ms",
    dl_warn_mbps: si0.dl_warn_mbps == null ? "not set" : fmt(si0.dl_warn_mbps, 0) + " Mbps",
    ul_warn_mbps: si0.ul_warn_mbps == null ? "not set" : fmt(si0.ul_warn_mbps, 0) + " Mbps",
  };
  document.getElementById("facts").innerHTML = facts(summary, localise);

  speedChart(speed, (summary.status_inputs || {}).dl_warn_mbps);
}
document.getElementById("facts").addEventListener("click", (e) => {
  const tile = e.target.closest(".fact.link");
  if (tile) showInfo(tile.dataset.metric);
});
const modal = document.getElementById("modal");
modal.addEventListener("click", () => modal.classList.remove("open"));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") modal.classList.remove("open");
});
load();
</script>
</body>
</html>
"""
