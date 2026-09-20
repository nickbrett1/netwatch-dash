"""The landing page: a drill-in that shows the history the tile only summarises.

The homepage tile answers "is it OK *now*" with five numbers. This page is what
clicking it opens: the same verdict, the numbers behind it, and the per-minute
history of the path -- all from endpoints the app already serves
(`/api/summary`, `/api/localise`, `/api/speed`), so the page costs three fetches
and no second parse of the csv.

It is a single self-contained document: inline CSS and JS, no CDN, no build step,
no template engine. The dashboard must render on a host with no network access
beyond its own tailnet, so anything it needs has to come from the app itself.

Two rendering rules this page keeps, both learned from the data contract:

* **An empty chart is not evidence of nothing.** Loss really is 0% across the
  window, so the loss chart has no bars to draw -- which looks identical to a
  broken chart. It says "no loss recorded" instead, and prints the sample count
  it is claiming that over.
* **A chart says what it is not showing.** The RTT series is clipped at the 98th
  percentile so that one gateway spike cannot flatten everything else onto the
  axis; when it clips, it says so and by how much.
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
  .legend.col { display: grid; gap: 4px 18px;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .legend.col .key { align-items: baseline; }
  .legend.col b { font-family: ui-monospace, Menlo, monospace; }
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
    <h2>Round-trip time, per minute (ms)</h2>
    <div class="legend" id="legend"></div>
    <svg id="chart" viewBox="0 0 1000 260" preserveAspectRatio="none"></svg>
    <p class="muted small" id="chart-note"></p>
  </section>
  <section>
    <h2>Loss per minute</h2>
    <svg id="loss" viewBox="0 0 1000 150" preserveAspectRatio="none"></svg>
    <p class="muted small" id="loss-note"></p>
  </section>
  <section>
    <h2>Daily bandwidth checks (Mbps)</h2>
    <div class="legend" id="speed-legend"></div>
    <svg id="speed" viewBox="0 0 1000 240" preserveAspectRatio="none"></svg>
    <p class="muted small" id="speed-note"></p>
  </section>
</main>
<script>
const COLORS = { net: "#5ee08a", wire: "#63b3ed", gw: "#f0c14b", wl: "#c084fc" };
const TARGETS = ["net", "wire", "gw", "wl"];
// Which host each series is, and what watching it tells you. The letters alone
// are not self-explanatory, and the legend is where that has to be fixed.
const TARGET_META = {
  net:  { host: "1.1.1.1",     what: "forwarded path — beyond the router" },
  wire: { host: "192.168.1.2", what: "wired peer — far end of the LAN link" },
  gw:   { host: "192.168.1.1", what: "the router itself" },
  wl:   { host: "192.168.1.14", what: "wireless peer — over Wi-Fi" },
};

// Long-form definitions, on demand. They are here rather than on the page
// because they are reference material: needed once, then noise.
const INFO = {
  forwarded: { title: "Forwarded path RTT",
    body: "ICMP round-trip time to 1.1.1.1, a public address beyond the router. " +
      "It is the only series that measures the whole path — this Mac, the LAN, " +
      "the router, the ISP and the internet — which is why it is the one that " +
      "moves the status. It is compared against the host's RTT_WARN_MS (4 ms)." },
  gateway: { title: "Gateway RTT",
    body: "ICMP round-trip time to 192.168.1.1, the router itself. Diagnostic " +
      "only: a spike here while the forwarded path stays flat is the router's own " +
      "ICMP handling, not a fault on the path, so it never moves the status " +
      "(RTT_ALERT is off on this host)." },
  loss: { title: "Path loss",
    body: "The share of probes in the window that got no reply, read from the " +
      "probe log rather than the ping stream, and reported per minute. A minute " +
      "in which nothing replied counts as 100% loss for that minute. Loss is worth " +
      "watching beside RTT because it is the signal that survives a link that is " +
      "saturated rather than broken." },
  en0: { title: "en0 errors",
    body: "Cumulative error counters for en0, the LAN interface, from the " +
      "producer's # iferrs markers (netstat -ib): input errors + output errors + " +
      "collisions. These are raw since-boot totals, not deltas — the dashboard " +
      "derives the change between markers. Zero is the expected reading." },
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

function showInfo(id) {
  const info = INFO[id];
  if (!info) return;
  document.getElementById("modal-title").textContent = info.title;
  document.getElementById("modal-body").textContent = info.body;
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
  const over = warn != null && si.forwarded_rtt_ms != null && si.forwarded_rtt_ms >= warn;
  const dww = si.dl_warn_mbps, uww = si.ul_warn_mbps;
  const host = localise.host || {};
  const errs = host.errors || {};

  const tiles = [
    fact("Forwarded path RTT", fmt(si.forwarded_rtt_ms, 1) + " ms",
         warn == null ? "no warn threshold set" : `warn ≥ ${fmt(warn, 1)} ms`,
         over ? "over" : "", "forwarded"),
    fact("Gateway RTT", fmt(si.gw_rtt_ms, 1) + " ms", "the router itself", "", "gateway"),
    fact("Path loss", fmt(si.loss_pct, 2) + " %",
         si.probe_age_s == null ? "" : `probe ${Math.round(si.probe_age_s)}s old`, "", "loss"),
    fact("Link", si.link_ok ? "up" : "down",
         (localise.host && localise.host.iface) || "", si.link_ok ? "good" : "bad"),
    fact("Down", fmt(si.dl_mbps, 1) + " Mbps",
         dww == null ? "" : `warn ≤ ${fmt(dww, 0)}`,
         dww != null && si.dl_mbps != null && si.dl_mbps < dww ? "over" : ""),
    fact("Up", fmt(si.ul_mbps, 1) + " Mbps",
         uww == null ? "" : `warn ≤ ${fmt(uww, 0)}`,
         uww != null && si.ul_mbps != null && si.ul_mbps < uww ? "over" : ""),
    fact("en0 errors", String(si.en0_errors == null ? "–" : si.en0_errors),
         si.en0_errors == null ? "no # iferrs in window" : "ierrs+oerrs+coll", "", "en0"),
    fact("Peer", fmt((localise.peer || {}).peer_ms, 1) + " ms",
         (localise.peer || {}).peer || "", "", "peer"),
  ];
  if (errs.rx_bytes != null) {
    tiles.push(fact("en0 since boot",
      (errs.rx_bytes / 1e9).toFixed(2) + " GB rx",
      (errs.tx_bytes / 1e9).toFixed(2) + " GB tx"));
  }
  return tiles.join("");
}

// ------------------------------------------------------------------ the charts

function series(targets, target) {
  const t = targets[target];
  if (!t || !t.minutes) return [];
  return t.minutes.map(m => ({ x: m.minute, y: m.rtt_ms_avg, n: m.n,
                               measured: m.measured, loss: m.loss,
                               loss_pct: m.loss_pct }));
}

function rttChart(localise) {
  const svg = document.getElementById("chart");
  const targets = localise.targets || {};
  const warn = (localise.thresholds || {}).rtt_warn_ms;
  const data = TARGETS.map(t => ({ t, pts: series(targets, t).filter(p => p.y != null) }))
                      .filter(d => d.pts.length);
  const note = document.getElementById("chart-note");
  if (!data.length) {
    // "Nothing to draw" has two causes worth telling apart: the rollup has not
    // read the csv yet, or it read it and no probe replied.
    const read = TARGETS.some(t => ((targets[t] || {}).minutes || []).length);
    text(svg, 10, 24, read ? "no RTT measured" : "no samples in the window");
    note.textContent = read
      ? "minutes are present but no RTT was measured — every probe in the window was lost"
      : "the rollup has read nothing yet — this is not an empty network";
    return;
  }
  const xs = data.flatMap(d => d.pts.map(p => p.x));
  const ys = data.flatMap(d => d.pts.map(p => p.y)).sort((a, b) => a - b);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const p98 = ys[Math.min(ys.length - 1, Math.floor(ys.length * 0.98))];
  // One gateway spike should not flatten every other line onto the axis.
  const ymax = Math.max(p98, warn ? warn * 1.2 : 0, 1);
  const clipped = ys.filter(y => y > ymax).length;
  const px = x => 44 + (x - x0) / Math.max(1, x1 - x0) * 936;
  const py = y => 234 - Math.min(y, ymax) / ymax * 210;

  svg.appendChild(ns("line", { x1: 44, y1: 234, x2: 980, y2: 234, stroke: "#23262f" }));
  text(svg, 6, 26, ymax.toFixed(0) + " ms");
  text(svg, 6, 236, "0");
  if (warn) {
    svg.appendChild(ns("line", { x1: 44, y1: py(warn), x2: 980, y2: py(warn),
      stroke: "#f0c14b", "stroke-width": 1, "stroke-dasharray": "4 4",
      opacity: 0.7 }));
    text(svg, 984, py(warn) + 4, `warn ${fmt(warn, 0)}`, { "text-anchor": "end",
      fill: "#f0c14b" });
  }
  const legend = document.getElementById("legend");
  legend.innerHTML = "";
  legend.className = "legend col";
  for (const d of data) {
    const pts = d.pts.map(p => `${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(" ");
    svg.appendChild(ns("polyline", { points: pts, fill: "none",
      stroke: COLORS[d.t], "stroke-width": 1.5, "stroke-linejoin": "round" }));
    const meta = TARGET_META[d.t] || { host: "", what: "" };
    const sw = document.createElement("span"); sw.className = "key";
    sw.innerHTML = `<span class="swatch" style="background:${COLORS[d.t]}"></span>` +
      `<b>${d.t}</b> ${meta.host} <span class="muted">— ${meta.what}</span>`;
    legend.appendChild(sw);
  }
  const w = localise.window || {};
  const span = `${new Date(x0 * 60000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` +
               ` – ${new Date(x1 * 60000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  note.textContent = `${span} · ${ys.length} samples` +
    (clipped ? ` · ${clipped} sample(s) above ${ymax.toFixed(0)} ms drawn at the top edge` : "") +
    (w.truncated ? " · window is a bounded tail, older minutes are not loaded" : "");
}

function lossChart(localise) {
  const svg = document.getElementById("loss");
  const targets = localise.targets || {};
  const laneH = 30, top = 8;
  let samples = 0, anyLoss = false;
  const all = TARGETS.map(t => ({ t, pts: series(targets, t) })).filter(d => d.pts.length);
  const xs = all.flatMap(d => d.pts.map(p => p.x));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const px = x => 44 + (x - x0) / Math.max(1, x1 - x0) * 936;
  const w = Math.max(1, 936 / Math.max(1, x1 - x0));

  all.forEach((d, i) => {
    const base = top + i * laneH + laneH - 8;
    text(svg, 6, base - 2, d.t, { fill: COLORS[d.t] });
    text(svg, 20, base - 2, (TARGET_META[d.t] || {}).host || "", { "font-size": 9 });
    svg.appendChild(ns("line", { x1: 44, y1: base, x2: 980, y2: base,
      stroke: "#23262f" }));
    for (const p of d.pts) {
      samples++;
      // Either a failed probe (n - measured) or a shed packet is loss worth drawing.
      const lost = (p.loss || 0) + Math.max(0, (p.n || 0) - (p.measured || 0));
      if (!lost && !p.loss_pct) continue;
      anyLoss = true;
      const pct = Math.max(p.loss_pct || 0, (lost / Math.max(1, p.n)) * 100);
      const h = Math.max(2, Math.min(laneH - 10, pct));
      const r = ns("rect", { x: px(p.x).toFixed(1), y: base - h, width: w.toFixed(1),
                             height: h, fill: "#ff6b6b" });
      const title = ns("title", {});
      title.textContent = `${d.t} ${new Date(p.x * 60000).toLocaleString()} — ` +
        `${lost}/${p.n} probes lost (${(p.loss_pct || 0).toFixed(1)}%)`;
      r.appendChild(title);
      svg.appendChild(r);
    }
  });
  const note = document.getElementById("loss-note");
  note.textContent = anyLoss
    ? "each bar is a minute with at least one lost probe; hover for the count"
    : `no loss recorded in ${samples} sampled minutes — the bars are absent because ` +
      `the counts are zero, not because the chart failed`;
}

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
  document.getElementById("facts").innerHTML = facts(summary, localise);

  rttChart(localise);
  lossChart(localise);
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
