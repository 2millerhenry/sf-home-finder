const syncButton = document.querySelector("#sync");
const help = document.querySelector("#help");
const status = document.querySelector("#status");
let activeTab;

function isSearchPage(url) {
  return typeof url === "string" && /^https:\/\/(?:www\.)?furnishedfinder\.com\/housing\//.test(url);
}

async function refresh() {
  [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const stored = await chrome.storage.local.get(["lastStatus", "lastSyncAt", "nextCheckAt"]);
  if (!isSearchPage(activeTab?.url)) {
    syncButton.disabled = true;
    help.textContent = "Open your Furnished Finder search results first, then reopen this panel.";
  }
  if (stored.lastStatus) status.textContent = stored.lastStatus;
}

syncButton.addEventListener("click", async () => {
  syncButton.disabled = true;
  syncButton.textContent = "Syncing…";
  status.textContent = "Reading the visible cards…";
  const result = await chrome.runtime.sendMessage({
    type: "useCurrentFurnishedFinderSearch",
    tabId: activeTab.id,
    searchUrl: activeTab.url
  });
  if (result?.ok) {
    status.textContent = `Done: ${result.added} new, ${result.updated || 0} existing listings refreshed.`;
    syncButton.textContent = "Search saved";
    return;
  }
  status.textContent = result?.error || "Could not reach the local monitor.";
  syncButton.disabled = false;
  syncButton.textContent = "Try again";
});

refresh().catch(() => { status.textContent = "Could not read this Chrome tab."; });
