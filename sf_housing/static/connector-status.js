// A connector check runs inside the background scan, so the page that started
// it was rendered before there was anything to say. Without this, the badge
// sits on "Testing" until the person reloads by hand -- which reads exactly
// like a connector that silently failed.
(function () {
  "use strict";

  var cards = document.querySelectorAll("[data-connector-card]");
  if (!cards.length) return;

  var watched = [];
  cards.forEach(function (card) {
    var key = card.getAttribute("data-connector-card");
    var badge = document.querySelector('[data-connector-badge="' + key + '"]');
    if (badge && badge.classList.contains("checking")) watched.push(key);
  });
  if (!watched.length) return;

  var STATES = [
    "not_configured", "configured_unverified", "checking", "working",
    "working_zero", "waiting_first_alert", "degraded",
    "authorization_expired", "quota_blocked", "disabled",
  ];

  function node(attr, key) {
    return document.querySelector("[" + attr + '="' + key + '"]');
  }

  function setText(attr, key, text) {
    var el = node(attr, key);
    if (!el) return;
    el.textContent = text || "";
    el.hidden = !text;
  }

  function show(key, on) {
    var el = node("data-connector-progress", key);
    if (el) el.hidden = !on;
  }

  watched.forEach(function (key) { show(key, true); });

  function apply(key, status) {
    var badge = node("data-connector-badge", key);
    if (badge) {
      STATES.forEach(function (name) { badge.classList.remove(name); });
      badge.classList.add(status.state);
      badge.textContent = status.label;
    }
    setText("data-connector-message", key, status.message);
    // A working connector's next step only restates its result; two lines
    // saying the same thing read as a page unsure of its own answer.
    setText("data-connector-next-step", key, status.needs_action ? status.next_step : "");
    var when = "";
    if (status.last_attempt_at) {
      when = "Last checked just now";
      if (status.observed_items) when += " · " + status.observed_items + " imported in total";
    }
    setText("data-connector-when", key, when);
  }

  // Real progress, from the scan's own figures rather than an animation that
  // only signals "something is happening".
  function paint(progress) {
    watched.forEach(function (key) {
      var box = node("data-connector-progress", key);
      if (!box) return;
      var fill = box.querySelector("[data-progress-fill]");
      var text = box.querySelector("[data-progress-text]");
      var clock = box.querySelector("[data-progress-elapsed]");
      var percent = Math.max(4, Math.min(100, Number(progress.percent) || 0));
      if (fill) fill.style.width = percent + "%";
      if (text) {
        text.textContent = progress.current_source
          ? "Reading " + progress.current_source + "…"
          : "Checking Facebook…";
      }
      if (clock) {
        var elapsed = Math.max(0, Math.round(Number(progress.elapsed_seconds) || 0));
        clock.textContent = progress.remaining_label || (elapsed + "s");
      }
    });
  }

  function tick() {
    fetch("/scan/status", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (p) { if (p) paint(p); })
      .catch(function () {});
  }

  // Give up rather than poll a stalled scan forever; five minutes is well past
  // the bounded check's own ceiling of about two.
  var deadline = Date.now() + 5 * 60 * 1000;

  // Stopping silently would leave the badge reading Testing with nothing
  // behind it, which is the failure this whole file exists to prevent.
  function giveUp() {
    watched.forEach(function (key) {
      show(key, false);
      setText(
        "data-connector-next-step",
        key,
        "This check has not reported back. Reload the page, and run it again if it still says Testing."
      );
    });
    watched = [];
  }

  function poll() {
    fetch("/alerts/connectors.json", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (states) {
        watched = watched.filter(function (key) {
          var status = states[key];
          if (!status) return true;
          apply(key, status);
          if (status.settled) show(key, false);
          return !status.settled;
        });
        if (!watched.length) return;
        if (Date.now() < deadline) window.setTimeout(poll, 3000);
        else giveUp();
      })
      .catch(function () {
        // A dropped request mid-scan is not worth reporting; try again, and
        // let the deadline end it if the server is genuinely gone.
        if (Date.now() < deadline) window.setTimeout(poll, 6000);
        else giveUp();
      });
  }

  tick();
  var ticker = window.setInterval(function () {
    if (!watched.length) window.clearInterval(ticker);
    else tick();
  }, 1000);
  window.setTimeout(poll, 1500);
})();
