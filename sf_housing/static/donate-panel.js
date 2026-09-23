(() => {
  const dialog = document.querySelector("[data-donate-dialog]");
  const frame = dialog?.querySelector("[data-donate-frame]");
  if (!dialog || !frame || typeof dialog.showModal !== "function") return;

  const loading = dialog.querySelector("[data-donate-loading]");

  // Nothing is fetched from Ko-fi until somebody asks for it. Opening the
  // dashboard has to tell them nothing at all.
  const load = () => {
    if (frame.getAttribute("src") === "about:blank") {
      loading?.removeAttribute("hidden");
      // The frame is lazy, so its empty about:blank does not settle until the
      // panel is first shown -- and that load event arrives a moment after the
      // click, which took the line down again before Ko-fi had sent anything.
      // An empty frame is same-origin and can be asked where it is; once Ko-fi
      // is in it, asking throws, and that is the answer.
      const uncover = () => {
        let blank = false;
        try {
          blank = frame.contentWindow.location.href === "about:blank";
        } catch (crossOrigin) {
          blank = false;
        }
        if (blank) return;
        loading?.setAttribute("hidden", "");
        frame.removeEventListener("load", uncover);
      };
      frame.addEventListener("load", uncover);
      frame.setAttribute("src", dialog.dataset.donateEmbed);
    }
  };

  // Every arrival at Ko-fi, however it was reached. Hung on the footer's click
  // handler instead, this missed the thank-you card entirely -- the card hands
  // over by event, so its button opened the panel on Ko-fi's real widget while
  // the app went on planning a second ask a week later. Fire and forget: a
  // write that fails must never take somebody's donation with it.
  const thanked = () => {
    fetch("/donation-card/thanks", {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => {});
  };

  const open = () => {
    thanked();
    load();
    // Two modal dialogs can stack, and a caller opening this one from another
    // is expected to have closed its own first. Guarded anyway, because
    // showModal on an open dialog throws and would take the click with it.
    if (!dialog.open) dialog.showModal();
  };

  // Every way in, not only the footer's. The thank-you card adds a second one
  // on the dashboard, and a panel that answered the first element on the page
  // would have left that button doing nothing.
  document.querySelectorAll("[data-donate-open]").forEach((link) => {
    link.addEventListener("click", (event) => {
      // A modified click is a deliberate "open this somewhere else". The panel
      // never opens, so this is the only chance to record that somebody went.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
        thanked();
        return;
      }
      event.preventDefault();
      open();
    });
  });

  // The thank-you card hands over here once its own exit has finished, so the
  // two surfaces never animate across each other. An event rather than a
  // global, so neither script has to know the other exists -- and if the card
  // is not on the page, nothing sends one.
  document.addEventListener("donate-open", open);

  dialog.addEventListener("click", (event) => {
    // The backdrop is the dialog itself; a click on the panel is not a click out.
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector("[data-donate-close]")?.addEventListener("click", () => dialog.close());
})();
