/* QuotaLens: theme toggle, "polled Ns ago" ticker, fragment refresh, link-lost watchdog. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "quotalens-theme";
  try {
    var saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  } catch (err) { /* private mode or blocked storage: follow the OS */ }

  function toggleTheme() {
    var osDark = matchMedia("(prefers-color-scheme: dark)").matches;
    var current = root.dataset.theme || (osDark ? "dark" : "light");
    var next = current === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch (err) { /* ignore */ }
  }

  function tick() {
    var el = document.getElementById("polled");
    if (!el) return;
    var ts = Number(el.dataset.ts);
    if (!ts) return;
    var age = Math.round(Date.now() / 1000 - ts);
    el.textContent = age < 120 ? "polled " + age + "s ago" : (el.dataset.fallback || el.textContent);
  }

  var failures = 0;
  function markLost() {
    failures += 1;
    if (failures < 2) return;
    if (!root.dataset.link) {
      var since = document.getElementById("lost-since");
      if (since) since.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      root.dataset.link = "lost";
    }
  }

  function refresh() {
    fetch("/api/dashboard/fragment", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (html) {
        document.getElementById("app").innerHTML = html;
        failures = 0;
        delete root.dataset.link;
      })
      .catch(markLost);
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.closest && ev.target.closest("#t")) toggleTheme();
  });
  document.addEventListener("DOMContentLoaded", function () {
    var every = (Number(document.body.dataset.refresh) || 15) * 1000;
    setInterval(tick, 1000);
    setInterval(refresh, every);
  });
})();
