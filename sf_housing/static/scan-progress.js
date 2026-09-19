(() => {
  const root = document.querySelector("[data-scan-progress]");
  if (!root) return;

  const title = root.querySelector("[data-scan-title]");
  const percentLabel = root.querySelector("[data-scan-percent]");
  const track = root.querySelector("[role='progressbar']");
  const bar = root.querySelector("[data-scan-bar]");
  const detail = root.querySelector("[data-scan-detail]");
  const note = root.querySelector("[data-scan-note]");
  let finished = false;
  let shownNote = -1;

  // What the scan is actually doing, for the three minutes it takes. Every one
  // of these is true of the run in progress rather than filler: a reader who
  // watches the whole rotation should come away knowing how their homes are
  // collected. Written as functions so the ones with numbers in them are the
  // live numbers, not the ones from when the page loaded.
  // The wording comes from the scanner, so the first paint and every update
  // after it say the same thing. Only the separator is decided here.
  const remainingLabel = (progress) => {
    const label = progress.remaining_label;
    return label ? ` · ${label}` : "";
  };

  const NOTES = [
    // The counters -- elapsed, sources, listings seen -- live in the line
    // above and are not repeated here. This one is the exception because how
    // many are new is the thing that line does not say.
    (p) => `${p.listings_added || 0} of them are new since your last check.`,
    () => "Ranking every home against your deal: budget, area, size and timing.",
    () => "A home that just misses your deal is kept in Near matches, not dropped.",
    () => "Anything a listing leaves unsaid becomes a check on the home, not a reason to drop it.",
    () => "Homes you have already been shown are updated rather than listed twice.",
    () => "Matching addresses, so one building on three sites stays one home.",
    () => "Each site is read one page at a time, so none of them starts turning us away.",
    () => "Homes appear as each source finishes. Nothing waits for the last one.",
    () => "Re-checking whether the homes on your shortlist are still going.",
  ];

  // Eight seconds a message, taken from the scan's own clock rather than a
  // timer of its own: the poll runs twice a second, and re-rendering the same
  // sentence restarts its fade and makes the line flicker.
  const rotateNote = (progress) => {
    if (!note) return;
    const elapsed = Number(progress.elapsed_seconds) || 0;
    const index = Math.floor(elapsed / 8) % NOTES.length;
    if (index === shownNote) return;
    shownNote = index;
    note.textContent = NOTES[index](progress);
    note.classList.remove("is-fresh");
    void note.offsetWidth;
    note.classList.add("is-fresh");
  };

  const update = (progress) => {
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    const sourceCount = Number(progress.sources_total) || 0;
    const completed = Number(progress.sources_completed) || 0;
    const elapsed = Number(progress.elapsed_seconds) || 0;
    const checked = Number(progress.listings_seen) || 0;

    title.textContent = progress.current_source
      ? `Checking ${progress.current_source}`
      : "Starting source checks";
    percentLabel.textContent = `${percent}%`;
    track.setAttribute("aria-valuenow", String(percent));
    bar.style.setProperty("--scan-progress", String(percent / 100));
    detail.textContent = `${elapsed}s elapsed${remainingLabel(progress)} · ${completed} of ${sourceCount} sources · ${checked} listings checked`;
    rotateNote(progress);

    if (!progress.running && !finished) {
      finished = true;
      const hasErrors = progress.status === "completed_with_errors";
      // Interrupted: the computer slept and the check stopped rather than run on.
      const stopped = progress.status === "failed" || progress.status === "interrupted";
      title.textContent = stopped
        ? "Check stopped before it could finish"
        : hasErrors
          ? "Check finished with a source error"
          : "Check complete";
      if (note) {
        note.textContent = stopped
          ? "Whatever was collected before it stopped has been kept."
          : `${progress.listings_seen || 0} listings read. Showing the best of them now.`;
      }
      if (progress.status === "completed" || hasErrors) {
        percentLabel.textContent = "100%";
        track.setAttribute("aria-valuenow", "100");
        bar.style.setProperty("--scan-progress", "1");
      }
      window.setTimeout(() => {
        const destination = new URL(window.location.href);
        destination.searchParams.delete("message");
        destination.searchParams.delete("scan");
        window.location.replace(destination);
      }, 1400);
    }
  };

  const poll = async () => {
    try {
      const response = await fetch("/scan/status", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`Progress request failed: ${response.status}`);
      update(await response.json());
    } catch (_error) {
      title.textContent = "Still checking sources";
    }
    if (!finished) window.setTimeout(poll, 500);
  };

  poll();
})();
