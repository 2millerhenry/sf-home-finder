// Opening a listing and copying a contact note behave the same wherever a
// listing is shown. This used to live inline in the results table, so a page
// that showed one listing on its own had the markup and none of the behaviour.
document.addEventListener("click", function (event) {
  const link = event.target.closest("a[data-mark-opened]");
  if (link) {
    const card = link.closest(".listing-row, .listing-card");
    if (card) card.classList.add("opened-listing");
    fetch(link.dataset.markOpened, { method: "POST", credentials: "same-origin", keepalive: true }).catch(function () {});
  }
  const copyButton = event.target.closest("button[data-copy-outreach]");
  if (!copyButton) return;
  const field = document.getElementById(copyButton.dataset.copyOutreach);
  if (!field) return;
  field.select();
  const copied = navigator.clipboard && navigator.clipboard.writeText
    ? navigator.clipboard.writeText(field.value)
    : Promise.reject();
  copied.catch(function () { document.execCommand("copy"); }).finally(function () {
    copyButton.textContent = "Copied";
    window.setTimeout(function () { copyButton.textContent = "Copy note"; }, 1300);
  });
});

// Starring, without rebuilding the board to do it.
//
// The form posts and the server answers with a redirect, so a star used to
// cost a full re-render of a list of hundreds -- about three seconds, with
// nothing on screen saying the click had landed. A star does not change which
// homes belong on the page, so the button can simply flip and the post can
// happen behind it. The form stays exactly as it was for anyone without
// JavaScript, and for the one place where a star does change the page: the
// starred list itself, where un-starring has to remove the card.
document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (!form.matches("[data-star-form]")) return;
  const button = form.querySelector(".save-button");
  if (!button) return;

  event.preventDefault();
  const field = form.elements.namedItem("status");
  const wanted = field ? field.value : "saved";
  const starred = wanted === "saved";
  // Read the form before touching it: the hidden field below is flipped to
  // what the next click should ask for, and posting that would undo the star
  // in the same breath as setting it.
  const payload = new FormData(form);

  // Flip first. The answer only ever confirms this, and waiting for it is the
  // delay this exists to remove.
  const previousLabel = button.textContent;
  const previousTitle = button.getAttribute("aria-label");
  button.textContent = starred ? "★ Starred" : "☆ Star";
  button.classList.toggle("saved", starred);
  button.setAttribute(
    "aria-label",
    starred ? "Remove from starred listings" : "Add to starred listings"
  );
  if (field) field.value = starred ? "active" : "saved";
  const row = form.closest(".listing-row, .listing-card");
  if (row) row.classList.toggle("saved-listing", starred);

  fetch(form.action, {
    method: "POST",
    body: payload,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then(function (response) {
      if (!response.ok) throw new Error("status " + response.status);
    })
    .catch(function () {
      // Put the button back rather than leave a star the server never took.
      button.textContent = previousLabel;
      button.classList.toggle("saved", !starred);
      if (previousTitle) button.setAttribute("aria-label", previousTitle);
      if (field) field.value = wanted;
      if (row) row.classList.toggle("saved-listing", !starred);
      button.after(
        Object.assign(document.createElement("span"), {
          className: "star-failed",
          role: "status",
          textContent: "Not saved — try again",
        })
      );
      window.setTimeout(function () {
        form.parentElement?.querySelector(".star-failed")?.remove();
      }, 4000);
    });
});

// Passing, without rebuilding the board to do it.
//
// Pass posts and takes a redirect back to the board, so it cost a full render
// of a list of hundreds -- about a second, with the row still sitting there
// the whole time. A passed home leaves the list being read and changes exactly
// one other thing on the page: the count in the heading. So the row can go and
// the post can happen behind it.
//
// The template marks the form only on the two views a passed home leaves, and
// only while they fit on one page. Past that a home from the next page moves
// up into the gap and only the server knows which one, so there the form stays
// an ordinary form and the reload is the honest answer.
document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (!form.matches("[data-pass-form]")) return;
  const row = form.closest(".listing-row");
  if (!row) return;

  event.preventDefault();
  const payload = new FormData(form);
  const count = document.querySelector("[data-result-count]");
  const before = count ? Number(count.dataset.resultCount) : null;

  const setCount = function (value) {
    if (!count || !Number.isFinite(value)) return;
    const noun = count.dataset.resultNoun || "home";
    count.dataset.resultCount = String(value);
    count.textContent = value.toLocaleString() + " " + noun + (value === 1 ? "" : "s");
  };

  // Out of the list first. Kept, not destroyed, so a refusal can put it back
  // exactly where it was rather than leaving a home the server still holds
  // missing from the page that is meant to show it.
  const anchor = document.createComment("passed");
  row.replaceWith(anchor);
  if (before !== null) setCount(before - 1);

  fetch(form.action, {
    method: "POST",
    body: payload,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then(function (response) {
      if (!response.ok) throw new Error("status " + response.status);
      anchor.remove();
    })
    .catch(function () {
      anchor.replaceWith(row);
      if (before !== null) setCount(before);
      const note = document.createElement("span");
      note.className = "star-failed";
      note.setAttribute("role", "status");
      note.textContent = "Not passed — try again";
      form.append(note);
      window.setTimeout(function () { note.remove(); }, 4000);
    });
});
