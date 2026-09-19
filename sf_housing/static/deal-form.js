(() => {
  const form = document.querySelector("[data-deal-form]");
  if (!form) return;

  const syncPath = (toggle) => {
    const panel = form.querySelector(`[data-path-fields="${toggle.dataset.pathToggle}"]`);
    if (!panel) return;
    const card = form.querySelector(`[data-path-card="${toggle.dataset.pathToggle}"]`);
    card?.classList.toggle("is-selected", toggle.checked);
    panel.querySelectorAll("input, select").forEach((field) => {
      field.disabled = !toggle.checked;
      if (field.matches("[data-path-required]")) field.required = toggle.checked;
    });
  };
  form.querySelectorAll("[data-path-toggle]").forEach((toggle) => {
    toggle.addEventListener("change", () => syncPath(toggle));
    syncPath(toggle);
  });

  // The optional price columns. Hidden by default so a home type asks for one
  // number, revealed on request, and revealed already for anyone who has set
  // one -- a value you cannot see is a value you cannot correct.
  const ledger = form.querySelector("[data-path-ledger]");
  const rangesToggle = form.querySelector("[data-ranges-toggle]");
  if (ledger && rangesToggle) {
    rangesToggle.addEventListener("click", () => {
      const shown = ledger.classList.toggle("show-ranges");
      rangesToggle.setAttribute("aria-expanded", String(shown));
      rangesToggle.textContent = shown ? "Hide price ranges" : "Add a price range";
    });
  }

  // The review section describes the deal the form holds right now. The
  // sentence comes from the profile itself rather than being rebuilt here, so
  // there is only ever one description of a deal. Responses can arrive out of
  // order, so only the newest one is allowed to win.
  const summaryTarget = form.querySelector("[data-deal-summary]");
  let summarySequence = 0;
  let summaryTimer = null;
  const refreshSummary = () => {
    if (!summaryTarget) return;
    const mine = ++summarySequence;
    fetch("/preferences/deal/preview", {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!data || mine !== summarySequence) return;
        // A form mid-edit is often not a valid deal yet. Keeping the last good
        // sentence is better than flashing an error at someone still typing.
        if (data.ok && data.summary) summaryTarget.textContent = data.summary;
        // The same reply carries what this deal would shortlist. Only the
        // newest one is allowed to land, for the same reason as the sentence:
        // a slower earlier request must not overwrite a faster later one.
        if (data.ok && data.counts) {
          document.dispatchEvent(
            new CustomEvent("cutoff-counts", {
              detail: {
                counts: data.counts,
                approximate: data.exact === false,
                pool: typeof data.pool === "number" ? data.pool : undefined,
                fromMarket: Boolean(data.fromMarket),
              },
            })
          );
        }
      })
      .catch(() => {});
  };
  const queueSummary = () => {
    window.clearTimeout(summaryTimer);
    summaryTimer = window.setTimeout(refreshSummary, 250);
  };
  form.addEventListener("input", queueSummary);
  form.addEventListener("change", queueSummary);

  const anywhere = form.querySelector("[data-anywhere-toggle]");
  const areaPicker = form.querySelector("[data-area-picker]");
  const areaTiers = [...form.querySelectorAll("[data-area-tier]")];
  const selectedAreaInputs = () => [...form.querySelectorAll("[data-area-value]")];
  const selectedAreas = () => new Set(selectedAreaInputs().map((input) => input.value));

  const announceAreaChange = () => {
    form.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const syncAreaOptions = () => {
    const allSelected = selectedAreas();
    areaTiers.forEach((tier) => {
      const select = tier.querySelector("[data-area-add]");
      const count = tier.querySelector("[data-area-count]");
      const own = new Set([...tier.querySelectorAll("[data-area-value]")].map((input) => input.value));
      const limit = Number(tier.dataset.areaLimit) || 5;
      const full = own.size >= limit;
      if (count) count.textContent = `${own.size} of ${limit}`;
      if (!select) return;
      select.disabled = Boolean(anywhere?.checked) || full;
      [...select.options].forEach((option) => {
        if (!option.value) return;
        option.disabled = full || (allSelected.has(option.value) && !own.has(option.value));
      });
    });
  };

  const removeArea = (event) => {
    const chip = event.currentTarget.closest(".area-chip");
    chip?.remove();
    syncAreaOptions();
    announceAreaChange();
  };

  const addArea = (event) => {
    const select = event.currentTarget;
    const area = select.value;
    const tier = select.closest("[data-area-tier]");
    if (!area || !tier) return;
    const limit = Number(tier.dataset.areaLimit) || 5;
    const own = [...tier.querySelectorAll("[data-area-value]")];
    if (own.length >= limit || selectedAreas().has(area)) {
      select.value = "";
      return;
    }
    const chip = document.createElement("li");
    chip.className = "area-chip";
    const label = document.createElement("span");
    label.textContent = area;
    const value = document.createElement("input");
    value.type = "hidden";
    value.name = `areas_${tier.dataset.areaTier}`;
    value.value = area;
    value.dataset.areaValue = "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "area-remove";
    remove.dataset.areaRemove = "";
    // A cross, matched to the one the page renders. The name stays in the
    // label, where a screen reader reads it and a cross would say nothing.
    remove.textContent = "\u00d7";
    remove.setAttribute("aria-label", `Remove ${area}`);
    remove.addEventListener("click", removeArea);
    chip.append(label, value, remove);
    tier.querySelector("[data-area-selected]")?.append(chip);
    select.value = "";
    syncAreaOptions();
    announceAreaChange();
  };

  areaTiers.forEach((tier) => {
    tier.querySelector("[data-area-add]")?.addEventListener("change", addArea);
    tier.querySelectorAll("[data-area-remove]").forEach((button) => button.addEventListener("click", removeArea));
  });

  const syncAreas = () => {
    if (!anywhere || !areaPicker) return;
    areaPicker.hidden = anywhere.checked;
    areaPicker.querySelectorAll("select, input, button").forEach((field) => { field.disabled = anywhere.checked; });
    if (!anywhere.checked) syncAreaOptions();
  };
  if (anywhere) anywhere.addEventListener("change", syncAreas);
  syncAreas();

  const flexible = form.querySelector("[data-flexible-toggle]");
  const dateFields = form.querySelector("[data-date-fields]");
  const syncDates = () => {
    if (!flexible || !dateFields) return;
    dateFields.hidden = flexible.checked;
    dateFields.querySelectorAll("input").forEach((field) => { field.disabled = flexible.checked; });
  };
  if (flexible) flexible.addEventListener("change", syncDates);
  syncDates();

  form.querySelectorAll("[data-split-total]").forEach((output) => {
    const key = output.dataset.splitTotal;
    const maximum = form.elements[`${key}_maximum`];
    const occupants = form.elements[`${key}_occupants`];
    const update = () => {
      const each = Number(maximum.value) || 0;
      const people = Number(occupants.value) || 0;
      output.textContent = each && people
        ? `$${(each * people).toLocaleString()} total ($${each.toLocaleString()} each for ${people}).`
        : "The total maximum is calculated from the per-person limit and group size.";
    };
    maximum.addEventListener("input", update);
    occupants.addEventListener("change", update);
    update();
  });

  const draftStatus = form.querySelector("[data-draft-status]");
  let draftTimer;
  const saveDraft = async () => {
    if (form.dataset.draftAutosave !== "1") return;
    if (draftStatus) draftStatus.textContent = "Saving your choices…";
    try {
      const response = await fetch("/preferences/draft", {
        method: "POST",
        body: new FormData(form),
        headers: { "X-SF-Housing-Request": "browser" },
      });
      if (draftStatus) draftStatus.textContent = response.ok
        ? "Your choices are saved on this Mac."
        : "Keep going; the complete choices will save when valid.";
    } catch (_) {
      if (draftStatus) draftStatus.textContent = "Draft save is waiting for the monitor to reconnect.";
    }
  };
  if (form.dataset.draftAutosave === "1") {
    form.addEventListener("input", () => {
      window.clearTimeout(draftTimer);
      draftTimer = window.setTimeout(saveDraft, 450);
    });
    form.addEventListener("change", () => {
      window.clearTimeout(draftTimer);
      draftTimer = window.setTimeout(saveDraft, 150);
    });
  }

  const submitButton = form.querySelector("[data-deal-submit]");
  const submitLabel = form.querySelector("[data-deal-submit-label]");
  const submitStatus = form.querySelector("[data-submit-status]");
  const savingNote = form.querySelector("[data-deal-saving-note]");

  // What saving a deal actually does, for the wait it actually takes. Every one
  // of these is true of the work in progress rather than filler: the post is
  // synchronous, so there is no percentage to show and the sentences are the
  // only thing that can explain why the page has not answered yet.
  const savingNotes = (homes) => [
    homes
      ? `Rescoring ${homes.toLocaleString()} homes you have already collected.`
      : "Rescoring the homes already collected.",
    "A score is your budget, your areas, the size and the timing, weighed together.",
    "Homes that no longer fit move to Near matches. Nothing is deleted.",
    "Areas you put in Dream count for more than the ones in Secondary.",
    "Anything a listing left unsaid becomes a Check on the home, not a reason to drop it.",
    "Saved homes and anything you have passed on keep their place.",
    "This runs once, here. The twice-daily checks do not wait for it.",
  ];

  let savingTimer = null;
  const restingLabel = submitLabel ? submitLabel.textContent : "";
  const restingStatus = submitStatus ? submitStatus.textContent : "";

  // Coming back with the Back button restores this page from the browser's
  // cache exactly as it was abandoned: panel sweeping, button dead, over a
  // form that is not being saved any more. Put it back to rest instead.
  const stopSavingPanel = () => {
    window.clearInterval(savingTimer);
    savingTimer = null;
    if (savingNote) savingNote.hidden = true;
    if (submitButton) {
      submitButton.disabled = false;
      delete submitButton.dataset.submitting;
      submitButton.removeAttribute("aria-busy");
    }
    if (submitLabel) submitLabel.textContent = restingLabel;
    if (submitStatus) submitStatus.textContent = restingStatus;
  };
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) stopSavingPanel();
  });

  const startSavingPanel = () => {
    // A second submit must not start a second clock over the same sentence.
    if (!savingNote || savingTimer) return;
    savingNote.hidden = false;
    const homes = Number(savingNote.dataset.listingCount) || 0;
    const lines = savingNotes(homes);
    let shown = -1;
    const show = () => {
      shown = (shown + 1) % lines.length;
      savingNote.textContent = lines[shown];
      savingNote.style.opacity = "1";
    };
    // Out, swap, back in. Reduced motion drops the transition in CSS, so the
    // same code simply swaps the sentence with no fade at all.
    const rotate = () => {
      savingNote.style.opacity = "0";
      window.setTimeout(show, 240);
    };
    show();
    // Eight seconds, the same beat as the scan panel, so the two waits in this
    // app read as one thing rather than two.
    savingTimer = window.setInterval(rotate, 8000);
  };
  form.addEventListener("submit", (event) => {
    // Clearing the deal posts this very form from its own button. It is not a
    // save, so none of what follows applies to it: the panel would announce a
    // rerank that is not happening, and the "choose a home type" gate below
    // used to make a half-filled deal impossible to clear at all.
    if (event.submitter && event.submitter.matches("[data-deal-reset]")) return;
    const first = form.querySelector("[data-path-toggle]");
    if (!form.querySelector("[data-path-toggle]:checked")) {
      event.preventDefault();
      first.setCustomValidity("Choose at least one kind of home.");
      first.reportValidity();
      first.addEventListener("change", () => first.setCustomValidity(""), { once: true });
    } else if (!form.checkValidity()) {
      event.preventDefault();
      form.reportValidity();
    } else if (submitButton) {
      const firstActivation = submitButton.dataset.firstActivation === "true";
      submitButton.disabled = true;
      submitButton.dataset.submitting = "true";
      submitButton.setAttribute("aria-busy", "true");
      if (submitLabel) submitLabel.textContent = firstActivation
        ? "Saving deal and starting your first check…"
        : "Saving and reranking homes…";
      if (submitStatus) submitStatus.textContent = firstActivation
        ? "Your search is being saved. The shortlist will show live check progress next."
        : "Your deal is being saved and every stored home is being reranked.";
      startSavingPanel();
    }
  });
})();
