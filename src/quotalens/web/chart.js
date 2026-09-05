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
    bindBoosts();
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
    // The crosshair is a continuous scrub; the boost mark is a discrete
    // annotation about one moment. Two readings of two different times, stacked,
    // is the reader's problem to untangle -- so only one is ever on screen. hide()
    // takes the vertical rule as well as the box: a line at 09:02 beside a
    // tooltip about 08:09 is the same contradiction in thinner ink.
    //
    // Driven from mousemove rather than the group's own mouseenter. An SVG <g>
    // reports enter/leave through the browser's hover chain, which does not
    // always update for grouped SVG children -- observed: the readout suppressed
    // correctly while mouseenter never fired. mousemove is the event that is
    // actually delivered, and one handler deciding what the pointer is over is
    // the simpler arrangement anyway.
    var over = ev.target.closest && ev.target.closest(".boost");
    if (over) { hide(); boostTip(over, true); return; }
    // ...and leaving the mark puts it away again. Without this the tip stays up
    // while the crosshair comes back, which is the same two-tooltips-two-times
    // collision the other way round.
    boostTip(null, false);
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
  /* ---- the boost tooltip ------------------------------------------------- */

  function boostTip(group, on) {
    var tip = document.getElementById("boost-tip");
    if (!tip || !state.svg) return;
    if (!on) { show(tip, false); return; }
    tip.textContent = group.getAttribute("data-detail") || "";
    show(tip, true);
    // Anchored to the mark's own rendered box, not to the pointer and not to
    // chart units. The pointer version is what put it on top of the crosshair
    // readout; chart units would need the scale factor applied to a pixel gap,
    // which is how the first attempt clipped the label it sits under.
    var box = group.getBoundingClientRect();
    // The tooltip is position:absolute, so its origin is its offsetParent -- the
    // chart <section> -- not the <svg> inside it. Measuring against the svg
    // under-counts by the section's padding, which is why the first attempt sat
    // on top of the label it is meant to hang below.
    var wrap = (tip.offsetParent || state.svg).getBoundingClientRect();
    var top = box.bottom - wrap.top + 6;
    // Flipped marks are right-aligned, so the tooltip mirrors with the label.
    var left = group.getAttribute("data-flip")
      ? box.right - wrap.left - tip.offsetWidth
      : box.left - wrap.left;
    if (top + tip.offsetHeight > wrap.height) top = box.top - wrap.top - tip.offsetHeight - 6;
    tip.style.left = Math.max(0, Math.min(left, wrap.width - tip.offsetWidth)) + "px";
    tip.style.top = Math.max(0, top) + "px";
  }

  function bindBoosts() {
    var groups = document.querySelectorAll("#chart .boost");
    for (var i = 0; i < groups.length; i++) {
      // Pointer hovering is handled in onMove; these two are the keyboard path.
      // tabindex="0" is on the group, so this costs four lines and makes the
      // detail reachable without a pointer, which <title> never was.
      (function (g) {
        g.addEventListener("focus", function () { boostTip(g, true); });
        g.addEventListener("blur", function () { boostTip(g, false); });
      })(groups[i]);
    }
  }

  function hide() {
    var tip = document.getElementById("boost-tip");
    if (tip) show(tip, false);
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
