(() => {
  const link = document.querySelector("[data-donate-open]");
  const dialog = document.querySelector("[data-donate-dialog]");
  const frame = dialog?.querySelector("[data-donate-frame]");
  if (!link || !dialog || !frame || typeof dialog.showModal !== "function") return;

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
      frame.setAttribute("src", link.dataset.donateEmbed);
    }
  };

  link.addEventListener("click", (event) => {
    // A modified click is a deliberate "open this somewhere else".
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    load();
    dialog.showModal();
  });

  dialog.addEventListener("click", (event) => {
    // The backdrop is the dialog itself; a click on the panel is not a click out.
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector("[data-donate-close]")?.addEventListener("click", () => dialog.close());
})();
