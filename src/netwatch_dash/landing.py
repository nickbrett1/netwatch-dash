"""The landing page: a drill-in that shows the history the tile only summarises.

The homepage tile answers "is it OK *now*" with five numbers. This page is what
clicking it opens: the same verdict, plus the per-minute history behind it —
the forwarded path against the gateway, drawn from the rollup the app already
holds in memory (`/api/localise`), so the page costs one fetch and no second
parse of the csv.

It is a single self-contained document: inline CSS and JS, no CDN, no build step,
no template engine. The dashboard must render on a host with no network access
beyond its own tailnet, so anything it needs has to come from the app itself.
"""

from __future__ import annotations

LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>netwatch-dash</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 14px/1.5 -apple-system, system-ui, sans-serif;
         background: #0f1117; color: #e6e6e6; }
  header { padding: 16px 20px; border-bottom: 1px solid #23262f; display: flex;
           gap: 16px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 16px; margin: 0; }
  .pill { padding: 2px 10px; border-radius: 999px; font-weight: 600; }
  .ok { background: #14351f; color: #5ee08a; }
  .warn { background: #3a2f10; color: #f0c14b; }
  .crit { background: #3a1518; color: #ff6b6b; }
  .unknown { background: #23262f; color: #9aa0aa; }
  main { padding: 20px; display: grid; gap: 20px; max-width: 1100px; }
  section { background: #161923; border: 1px solid #23262f; border-radius: 10px;
            padding: 16px; }
  h2 { margin: 0 0 10px; font-size: 13px; text-transform: uppercase;
       letter-spacing: .06em; color: #9aa0aa; }
  ul { margin: 0; padding-left: 18px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 8px; }
  .key { display: flex; gap: 6px; align-items: center; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; }
  svg { width: 100%; height: 260px; display: block; }
  .muted { color: #6b7280; }
</style>
</head>
<body>
<header>
  <h1>netwatch-dash</h1>
  <span id="status" class="pill unknown">…</span>
  <span id="version" class="muted"></span>
</header>
<main>
  <section>
    <h2>Why this status</h2>
    <ul id="reasons"><li class="muted">loading…</li></ul>
  </section>
  <section>
    <h2>Round-trip time, per minute (ms)</h2>
    <div class="legend" id="legend"></div>
    <svg id="chart" viewBox="0 0 1000 260" preserveAspectRatio="none"></svg>
    <p class="muted" id="window"></p>
  </section>
  <section>
    <h2>Loss per minute</h2>
    <svg id="loss" viewBox="0 0 1000 120" preserveAspectRatio="none"></svg>
  </section>
</main>
<script>
const COLORS = { net: "#5ee08a", wire: "#63b3ed", gw: "#f0c14b", wl: "#c084fc" };
const TARGETS = ["net", "wire", "gw", "wl"];

function ns(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

function series(targets, target) {
  const t = targets[target];
  if (!t || !t.minutes) return [];
  return t.minutes.filter(m => m.rtt_ms_avg != null)
                 .map(m => ({ x: m.minute, y: m.rtt_ms_avg, loss: m.loss_pct }));
}

function rttChart(targets) {
  const svg = document.getElementById("chart");
  const data = TARGETS.map(t => ({ t, pts: series(targets, t) }))
                      .filter(d => d.pts.length);
  const legend = document.getElementById("legend");
  if (!data.length) { svg.appendChild(ns("text", { x: 10, y: 24, fill: "#6b7280" }))
                         .textContent = "no samples in the window"; return; }
  const xs = data.flatMap(d => d.pts.map(p => p.x));
  const ys = data.flatMap(d => d.pts.map(p => p.y));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = 0, y1 = Math.max(...ys) * 1.1 || 1;
  const px = x => 40 + (x - x0) / Math.max(1, x1 - x0) * 940;
  const py = y => 240 - (y - y0) / (y1 - y0) * 220;
  svg.appendChild(ns("line", { x1: 40, y1: 240, x2: 980, y2: 240, stroke: "#23262f" }));
  for (const d of data) {
    const pts = d.pts.map(p => `${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(" ");
    svg.appendChild(ns("polyline", { points: pts, fill: "none",
      stroke: COLORS[d.t], "stroke-width": 1.5 }));
    const sw = document.createElement("span"); sw.className = "key";
    sw.innerHTML = `<span class="swatch" style="background:${COLORS[d.t]}"></span>${d.t}`;
    legend.appendChild(sw);
  }
  svg.appendChild(ns("text", { x: 4, y: 16, fill: "#6b7280", "font-size": 11 }))
     .textContent = y1.toFixed(0) + " ms";
}

function lossChart(targets) {
  const svg = document.getElementById("loss");
  const pts = series(targets, "net");
  if (!pts.length) return;
  const xs = pts.map(p => p.x); const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const w = 940 / Math.max(1, x1 - x0);
  for (const p of pts) {
    if (!p.loss) continue;
    const h = Math.max(2, Math.min(100, p.loss));
    svg.appendChild(ns("rect", { x: 40 + (p.x - x0) * w, y: 110 - h, width: w,
      height: h, fill: "#ff6b6b" }));
  }
  svg.appendChild(ns("line", { x1: 40, y1: 110, x2: 980, y2: 110, stroke: "#23262f" }));
}

async function load() {
  const [summary, localise] = await Promise.all([
    fetch("/api/summary").then(r => r.json()),
    fetch("/api/localise").then(r => r.json()),
  ]);
  const status = document.getElementById("status");
  status.textContent = summary.status;
  status.className = "pill " + summary.status;
  document.getElementById("version").textContent = "v" + (summary.version || "?");
  const reasons = document.getElementById("reasons");
  reasons.innerHTML = "";
  for (const r of summary.status_reason || []) {
    const li = document.createElement("li"); li.textContent = r; reasons.appendChild(li);
  }
  rttChart(localise.targets || {});
  lossChart(localise.targets || {});
  const w = localise.window || {};
  document.getElementById("window").textContent =
    `window: ${w.minutes || 0} minutes · read ${w.read ? "yes" : "not yet"}` +
    (w.truncated ? " · partial (bounded tail)" : "");
}
load();
</script>
</body>
</html>
"""
