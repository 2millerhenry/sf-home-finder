(() => {
  const card = document.querySelector("[data-thanks-card]");
  if (!card || typeof card.showModal !== "function") return;

  // Never over a running check. The route refuses this as well, because it can
  // see a scan started in another tab or by the scheduler -- but the page in
  // front of this reader is already given over to the progress panel, and
  // asking for money across it would be the worst moment the card could pick.
  if (document.querySelector("[data-scan-progress]")) return;

  // Long enough for the board to have painted and been looked at, short enough
  // that the card still belongs to the act of opening the app rather than
  // arriving out of nowhere over something being read.
  const SETTLE_MS = 2000;
  // An exit that never reports back must not leave the card sitting over the
  // board. Comfortably longer than the 240ms the stylesheet spends on it.
  const EXIT_CEILING_MS = 700;

  const stillness = window.matchMedia("(prefers-reduced-motion: reduce)");

  const record = (path) => {
    // Fire and forget, and never let a failed write take the click with it.
    // The worst a lost one costs is an ask that was already going to be the
    // last: the count was written by the route that showed this card.
    fetch(path, { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => {});
  };

  // The card goes before anything replaces it, so the two surfaces are never
  // on screen together. The promise settles once it is really gone.
  let leaving = false;
  const leave = (kind) =>
    new Promise((gone) => {
      if (leaving) return;
      leaving = true;
      card.dataset.leaving = kind;
      let finished = false;
      const finish = (event) => {
        // The surface is the only thing animating on the way out, but the
        // contents animate on the way in and those events bubble to the same
        // element.
        if (event && event.target !== card) return;
        if (finished) return;
        finished = true;
        card.removeEventListener("animationend", finish);
        delete card.dataset.leaving;
        card.close();
        leaving = false;
        gone();
      };
      if (stillness.matches) {
        finish();
        return;
      }
      card.addEventListener("animationend", finish);
      // A tab put to sleep mid-exit never fires animationend at all.
      window.setTimeout(finish, EXIT_CEILING_MS);
    });

  const show = () => {
    // Somebody who pressed the footer's button in the last two seconds is
    // already at the donation panel. Two modal dialogs stack, and the one
    // nobody asked for would be the one on top.
    if (document.querySelector("[data-donate-dialog]")?.open) return;
    card.showModal();
  };

  const ask = async () => {
    try {
      const response = await fetch("/donation-card", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const answer = await response.json();
      if (answer && answer.due) show();
    } catch (_unreachable) {
      // The board is what this page is for. A card that could not be asked
      // about is simply a card that does not appear.
    }
  };

  // A dashboard that opened into a background tab -- a pinned tab restored
  // with the browser, a link opened behind what somebody is reading -- would
  // otherwise spend one of the two asks on a card nobody was looking at.
  const askWhenWatched = () => {
    if (document.visibilityState !== "visible") {
      document.addEventListener("visibilitychange", askWhenWatched, { once: true });
      return;
    }
    ask();
  };

  card.querySelector("[data-thanks-yes]")?.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
      // A deliberate "open this in a tab of its own". It is still somebody
      // going to the donation page, and the panel that would have recorded
      // that never opens, so this records it instead.
      record("/donation-card/thanks");
      return;
    }
    event.preventDefault();
    // Not recorded here: the panel records every arrival at Ko-fi, wherever it
    // was opened from, and writing it twice would be two ways to be wrong.
    leave("yes").then(() => document.dispatchEvent(new CustomEvent("donate-open")));
  });

  const dismiss = () => leave("no");
  card.querySelector("[data-thanks-no]")?.addEventListener("click", dismiss);
  card.querySelector("[data-thanks-close]")?.addEventListener("click", dismiss);
  // The backdrop is the dialog itself; a click on the card is not a click out.
  card.addEventListener("click", (event) => {
    if (event.target === card) dismiss();
  });
  // Escape closes a modal dialog outright, which would cut the exit off at the
  // first frame. This takes the close back and plays it properly.
  card.addEventListener("cancel", (event) => {
    event.preventDefault();
    dismiss();
  });

  window.setTimeout(askWhenWatched, SETTLE_MS);
})();
