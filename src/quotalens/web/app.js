/* QuotaLens page script: theme, polled-ago ticker, fragment refresh, view controls, URL state.
   The page is server-rendered; everything here is progressive enhancement. */
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

  /* ---- view state lives in the URL ------------------------------------- */
  function currentQuery() {
    return location.search.replace(/^\?/, "");
  }
  function setQuery(query, push) {
    var url = query ? "/?" + query : "/";
    if (push) history.pushState({ q: query }, "", url); else history.replaceState({ q: query }, "", url);
  }
  function fragmentUrl() {
    var q = currentQuery();
    return "/api/dashboard/fragment" + (q ? "?" + q : "");
  }

  /* ---- polled-ago ticker ----------------------------------------------- */
  function tick() {
    var el = document.getElementById("polled");
    if (!el) return;
    var ts = Number(el.dataset.ts);
    if (!ts) return;
    var age = Math.round(Date.now() / 1000 - ts);
    el.textContent = age < 120 ? "polled " + age + "s ago" : (el.dataset.fallback || el.textContent);
  }

  /* ---- fragment refresh and the link-lost watchdog ---------------------- */
  var failures = 0;
  var timer = null;
  function markLost() {
    failures += 1;
    if (failures < 2) return;
    if (!root.dataset.link) {
      var since = document.getElementById("lost-since");
      if (since) since.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
      root.dataset.link = "lost";
    }
  }
  function refresh() {
    return fetch(fragmentUrl(), { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (html) {
        var active = document.activeElement;
        var focusKey = active && active.dataset ? (active.dataset.range || active.dataset.lookback || active.dataset.refresh || active.dataset.series) : null;
        var focusAttr = active && active.dataset ? Object.keys(active.dataset)[0] : null;
        document.getElementById("app").innerHTML = html;
        failures = 0;
        delete root.dataset.link;
        if (focusKey && focusAttr) {
          var again = document.querySelector("[data-" + focusAttr + '="' + focusKey + '"]');
          if (again) again.focus();
        }
        document.dispatchEvent(new CustomEvent("quotalens:rendered"));
        schedule();
      })
      .catch(markLost);
  }
  function schedule() {
    if (timer) clearInterval(timer);
    timer = null;
    var seconds = Number(document.body.dataset.refresh || 0);
    var chosen = document.querySelector("[data-refresh][aria-current]");
    if (chosen) {
      var key = chosen.dataset.refresh;
      seconds = key === "off" ? 0 : key === "1m" ? 60 : key === "5m" ? 300 : parseInt(key, 10) || seconds;
    }
    if (seconds > 0) timer = setInterval(refresh, seconds * 1000);
  }

  /* ---- controls: every control is a real link or form; we just avoid a full load ---- */
  function navigate(href, push) {
    var q = href.indexOf("?") >= 0 ? href.slice(href.indexOf("?") + 1) : "";
    setQuery(q, push);
    return refresh();
  }
  function pollNow(form) {
    var button = form.querySelector("button");
    if (button) button.disabled = true;
    fetch("/api/poll", { method: "POST" })
      .then(function (res) { return res.json(); })
      .then(function () { return refresh(); })
      .catch(markLost)
      .then(function () { if (button) button.disabled = false; });
  }

  document.addEventListener("click", function (ev) {
    var target = ev.target.closest ? ev.target.closest("#t, a.rb, a.el-link, a.sess, th a[data-sort]") : null;
    if (!target) return;
    if (target.id === "t") { toggleTheme(); return; }
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return; // let new-tab clicks through
    ev.preventDefault();
    navigate(target.getAttribute("href"), true);
  });
  document.addEventListener("submit", function (ev) {
    if (ev.target && ev.target.id === "poll-form") {
      ev.preventDefault();
      pollNow(ev.target);
    }
  });
  window.addEventListener("popstate", function () { refresh(); });
  window.quotalens = { navigate: navigate, refresh: refresh };

  document.addEventListener("DOMContentLoaded", function () {
    setInterval(tick, 1000);
    schedule();
  });
})();
