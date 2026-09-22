// A connector test runs inside the background scan, so the page that started
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

  function setText(selector, key, text) {
    var node = document.querySelector('[' + selector + '="' + key + '"]');
    if (!node) return;
    if (text) {
      node.textContent = text;
      node.hidden = false;
    } else {
      node.textContent = "";
      node.hidden = true;
    }
  }

  function apply(key, status) {
    var badge = document.querySelector('[data-connector-badge="' + key + '"]');
    if (badge) {
      STATES.forEach(function (name) { badge.classList.remove(name); });
      badge.classList.add(status.state);
      badge.textContent = status.label;
    }
    setText("data-connector-message", key, status.message);
    setText("data-connector-next-step", key, status.next_step);
    var when = "";
    if (status.last_attempt_at) {
      when = "Last checked just now";
      if (status.observed_items) {
        when += " · " + status.observed_items + " listing" +
          (status.observed_items === 1 ? "" : "s") + " imported so far";
      }
    }
    setText("data-connector-when", key, when);
  }

  // Give up rather than poll a stalled scan forever; five minutes is well past
  // the bounded check's own ceiling of about two.
  var deadline = Date.now() + 5 * 60 * 1000;

  // Stopping silently would leave the badge reading Testing with nothing
  // behind it, which is the failure this whole file exists to prevent.
  function giveUp() {
    watched.forEach(function (key) {
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

  window.setTimeout(poll, 2000);
})();
