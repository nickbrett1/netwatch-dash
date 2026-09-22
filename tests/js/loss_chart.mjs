// Run the landing page's inline chart script for real, and print what it drew.
//
// The chart code is a string inside landing.py, so a Python test cannot call it
// and a substring assertion cannot see geometry — which is how the loss chart
// shipped with no time axis, its lane labels over the plot, and bars sixty times
// too wide. This gives the chart just enough DOM to execute (it only reaches for
// document.getElementById/createElement(NS) and appendChild), feeds it a fixed
// payload, and prints the SVG.
//
// Usage: node loss_chart.mjs <path-to-extracted-script.js> [full|empty]
import fs from "fs";

const chartPath = process.argv[2];
const mode = process.argv[3] || "full";

const els = {};
function mkEl(tag) {
  return {
    tag, attrs: {}, children: [], textContent: "", innerHTML: "",
    className: "", style: {}, dataset: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
  };
}
globalThis.document = {
  getElementById(id) { return els[id] || (els[id] = mkEl("div#" + id)); },
  createElementNS(_ns, tag) { return mkEl(tag); },
  createElement(tag) { return mkEl(tag); },
  addEventListener() {},
};

// Five one-minute slices. Every target is fully measured except where noted:
//   net  loses 4 of 12 in minute +2   (a third of the lane)
//   gw   loses 12 of 12 in minute +0  (full height)
//   wl   loses 1 of 12 in minute +1   (below a pixel; must hit the floor)
const M0 = 30000000;
const mk = (minute, n, loss) => ({
  minute, n, measured: n - loss, loss, loss_pct: (100 * loss) / n,
  rtt_ms_avg: loss === n ? null : 5.0, rtt_ms_min: 4.0, rtt_ms_max: 9.0, bucket_s: 60,
});
const rows = (spec) => spec.map(([dm, n, loss]) => mk(M0 + dm, n, loss));

const targets = mode === "empty" ? {} : {
  net:  { series: rows([[0, 12, 0], [1, 12, 0], [2, 12, 4], [3, 12, 0], [4, 12, 0]]) },
  wire: { series: rows([[0, 12, 0], [1, 12, 0], [2, 12, 0], [3, 12, 0], [4, 12, 0]]) },
  gw:   { series: rows([[0, 12, 12]]) },
  wl:   { series: rows([[0, 12, 0], [1, 12, 1], [2, 12, 0]]) },
};
const payloads = {
  "/api/localise": { targets, thresholds: { rtt_warn_ms: 25, rtt_excess_ms: 10 }, window: {} },
  "/api/summary": { status: "ok", status_inputs: {} },
  "/api/speed": { speeds: [] },
};
globalThis.fetch = (url) => Promise.resolve({ json: () => Promise.resolve(payloads[url]) });

eval(fs.readFileSync(chartPath, "utf8"));
// load() is async; let its three awaits settle before reading the DOM.
await new Promise((resolve) => setTimeout(resolve, 20));

const svg = els["loss"];
const num = (v) => Number(v);
const out = {
  texts: svg.children.filter((c) => c.tag === "text")
    .map((c) => ({ x: num(c.attrs.x), y: num(c.attrs.y), s: c.textContent })),
  lines: svg.children.filter((c) => c.tag === "line")
    .map((c) => ({ x1: num(c.attrs.x1), y1: num(c.attrs.y1), x2: num(c.attrs.x2), y2: num(c.attrs.y2) })),
  rects: svg.children.filter((c) => c.tag === "rect")
    .map((c) => ({ x: num(c.attrs.x), y: num(c.attrs.y), w: num(c.attrs.width), h: num(c.attrs.height),
                   title: (c.children[0] || {}).textContent || "" })),
  note: els["loss-note"].textContent,
};
console.log(JSON.stringify(out));
