/* Alert-aware Monitor link for the Chainlit header.
 *
 * Save as public/custom.js and enable in .chainlit/config.toml:
 *     [UI]
 *     custom_js  = "/public/custom.js"
 *     custom_css = "/public/custom.css"
 *
 * Chainlit is a React SPA: the header does not exist at DOMContentLoaded,
 * and React wipes our classes whenever it re-renders. So we (a) poll for
 * the link until it appears, and (b) watch the DOM and re-apply the last
 * known state every time it comes back.
 */

(function () {
  "use strict";

  var POLL_MS = 15000; // how often to ask the monitor API (= monitor poll)
  var FIND_MS = 300; // how often to look for the link at startup
  var LINK_SELECTOR = 'a[href="/monitor/"]';
  var CLASSES = ["mon-critical", "mon-warning", "mon-clear"];

  // Last known state, so a re-render can be repainted without a refetch.
  var state = { cls: null, count: "", title: "Monitor" };

  function endpoint(severity) {
    return (
      "/monitor/api/issues" +
      "?status=ACTIVE" +
      "&acknowledgment=UNACKNOWLEDGED" +
      "&severity=" +
      severity +
      "&limit=100"
    );
  }

  function countFor(severity) {
    return fetch(endpoint(severity), { credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        return Array.isArray(data) ? data.length : 0;
      });
  }

  /** Paint the cached state onto the link, if it is currently in the DOM. */
  function paint() {
    var link = document.querySelector(LINK_SELECTOR);
    if (!link) return false;

    CLASSES.forEach(function (c) {
      link.classList.remove(c);
    });
    if (state.cls) link.classList.add(state.cls);

    link.setAttribute("data-count", state.count);
    link.setAttribute("title", state.title);
    return true;
  }

  function poll() {
    Promise.all([countFor("CRITICAL"), countFor("WARNING")])
      .then(function (counts) {
        var critical = counts[0];
        var warning = counts[1];
        var total = critical + warning;

        state.cls =
          critical > 0
            ? "mon-critical"
            : warning > 0
              ? "mon-warning"
              : "mon-clear";
        // The badge counts what the colour means: criticals when there
        // are any, otherwise warnings. The title carries both.
        var shown = critical > 0 ? critical : warning;
        state.count = shown > 0 ? String(shown) : "";
        state.title =
          total === 0
            ? "Monitor - no unacknowledged issues"
            : critical + " critical, " + warning + " warning (unacknowledged)";

        paint();
      })
      .catch(function (err) {
        // Monitor unreachable: go neutral rather than show a false all-clear.
        state.cls = null;
        state.count = "";
        state.title = "Monitor - status unavailable";
        paint();
        console.warn("Monitor poll failed:", err);
      });
  }

  function start() {
    // Fetch straight away so the first paint is not 30s late.
    poll();

    // Keep looking for the header link until React renders it.
    var finder = setInterval(function () {
      if (paint()) clearInterval(finder);
    }, FIND_MS);
    setTimeout(function () {
      clearInterval(finder);
    }, 60000); // stop hunting after a minute

    // Re-apply after any React re-render that replaces the header.
    var observer = new MutationObserver(function () {
      var link = document.querySelector(LINK_SELECTOR);
      if (link && state.cls && !link.classList.contains(state.cls)) {
        paint();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    setInterval(poll, POLL_MS);

    // Refresh on tab focus, so you are not looking at stale state.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) poll();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
