/* QuotaLens chart interaction: hover crosshair with a tabular readout, drag-to-zoom,
   double-click to reset. Series toggling is the end-of-line label links, handled in app.js. */
(function () {
  "use strict";
  var state = { data: null, svg: null, drag: null };

  function load() {
    var node = document.getElementById("chart-data");
    var svg = document.getElementById("chart");
    if (!node || !svg) { state.data = null; state.svg = null; return; }
    try { state.data = JSON.parse(node.textContent); } catch (err) { state.data = null; return; }
    state.svg = svg;
    svg.addEventListener("mousemove", onMove);
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("mousedown", onDown);
    svg.addEventListener("dblclick", onReset);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("mousemove", onDrag);
  }

  /* SVG elements have no .hidden property: toggle the attribute itself */
  function show(el, on) {
    if (!el) return;
    if (on) el.removeAttribute("hidden"); else el.setAttribute("hidden", "");
  }

  /* pixel x (client) -> chart x in viewBox units */
  function toChartX(clientX) {
    var rect = state.svg.getBoundingClientRect();
    return (clientX - rect.left) / rect.width * state.data.w;
  }
  function plotWidth() { return state.data.w - state.data.l - state.data.r; }
  function xToTs(x) {
    var d = state.data;
    var frac = (x - d.l) / plotWidth();
    return Math.round(d.start + Math.max(0, Math.min(1, frac)) * (d.end - d.start));
  }
  function tsToX(ts) {
    var d = state.data;
    return d.l + (ts - d.start) / (d.end - d.start) * plotWidth();
  }
  function fmtClock(ts) {
    var dt = new Date(ts * 1000);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var day = state.data.end - state.data.start > 2 * 86400 ? pad(dt.getDate()) + "/" + pad(dt.getMonth() + 1) + " " : "";
    return day + pad(dt.getHours()) + ":" + pad(dt.getMinutes());
  }
  /* nearest sample at or before ts, if within a bucket-ish tolerance */
  function valueAt(series, ts) {
    var pts = series.pts;
    if (!pts.length) return null;
    var lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (pts[mid][0] <= ts) lo = mid; else hi = mid - 1;
    }
    var best = pts[lo];
    if (lo + 1 < pts.length && Math.abs(pts[lo + 1][0] - ts) < Math.abs(best[0] - ts)) best = pts[lo + 1];
    var tolerance = Math.max(120, (state.data.end - state.data.start) / 300);
    return Math.abs(best[0] - ts) <= tolerance ? best : null;
  }

  function onMove(ev) {
    if (!state.data || state.drag) return;
    var x = toChartX(ev.clientX);
    var d = state.data;
    if (x < d.l || x > d.w - d.r) { hide(); return; }
    var ts = xToTs(x);
    var hover = document.getElementById("hover");
    var line = hover && hover.querySelector("line");
    var box = document.getElementById("readout");
    if (!hover || !line || !box) return;
    var snapX = x;
    var rows = [];
    d.series.forEach(function (s) {
      var pt = valueAt(s, ts);
      if (!pt) return;
      snapX = tsToX(pt[0]);
      rows.push('<span class="rk" style="color:var(--s' + s.slot + ')">' + esc(s.label) + "</span><span>" + fmt(pt[1]) + "%</span>");
    });
    line.setAttribute("x1", snapX.toFixed(1));
    line.setAttribute("x2", snapX.toFixed(1));
    show(hover, true);
    show(box, true);
    box.innerHTML = '<span class="rt">' + fmtClock(ts) + "</span>" + rows.join("");
    var rect = state.svg.getBoundingClientRect();
    var px = (snapX / d.w) * rect.width;
    box.style.left = Math.min(px + 12, rect.width - box.offsetWidth - 8) + "px";
    box.style.top = Math.max(0, ev.clientY - rect.top - box.offsetHeight - 12) + "px";
  }
  function hide() {
    var hover = document.getElementById("hover");
    var box = document.getElementById("readout");
    show(hover, false);
    show(box, false);
  }
  function fmt(v) { return Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  /* ---- drag to zoom ------------------------------------------------------ */
  function onDown(ev) {
    if (!state.data || ev.button !== 0) return;
    if (ev.target.closest && ev.target.closest("a")) return; // label links keep their click
    state.drag = { x0: toChartX(ev.clientX), x1: toChartX(ev.clientX) };
    var sel = document.getElementById("sel");
    if (sel) { show(sel, true); sel.setAttribute("x", state.drag.x0.toFixed(1)); sel.setAttribute("width", "0"); }
    hide();
    ev.preventDefault();
  }
  function onDrag(ev) {
    if (!state.drag) return;
    state.drag.x1 = toChartX(ev.clientX);
    var sel = document.getElementById("sel");
    if (!sel) return;
    var a = Math.min(state.drag.x0, state.drag.x1), b = Math.max(state.drag.x0, state.drag.x1);
    sel.setAttribute("x", a.toFixed(1));
    sel.setAttribute("width", (b - a).toFixed(1));
  }
  function onUp() {
    if (!state.drag) return;
    var drag = state.drag;
    state.drag = null;
    var sel = document.getElementById("sel");
    show(sel, false);
    var a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
    if (b - a < 6) return; // a click, not a selection
    var from = xToTs(a), to = xToTs(b);
    if (to - from < 60) return;
    var params = new URLSearchParams(location.search);
    params.set("range", from + "-" + to);
    window.quotalens.navigate("/?" + params.toString(), true);
  }
  function onReset(ev) {
    if (!state.data) return;
    ev.preventDefault();
    var params = new URLSearchParams(location.search);
    params.delete("range");
    var q = params.toString();
    window.quotalens.navigate(q ? "/?" + q : "/", true);
  }

  document.addEventListener("DOMContentLoaded", load);
  document.addEventListener("quotalens:rendered", load);
})();
