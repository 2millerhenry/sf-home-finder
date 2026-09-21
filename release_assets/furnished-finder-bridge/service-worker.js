const LOCAL_IMPORT_URL = "http://127.0.0.1:8000/integrations/furnished-finder/import";
const LOCAL_HEARTBEAT_URL = "http://127.0.0.1:8000/integrations/furnished-finder/heartbeat";
const BRIDGE_VERSION = chrome.runtime.getManifest().version;
const ALARM_NAME = "furnished-finder-next-check";
const PACIFIC_TIME_ZONE = "America/Los_Angeles";
const IMPORT_RETRY_DELAY_MS = 15_000;
const IMPORT_RETRY_ATTEMPTS = 9;

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function pacificParts(date) {
  const values = new Intl.DateTimeFormat("en-US", {
    timeZone: PACIFIC_TIME_ZONE,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    hourCycle: "h23"
  }).formatToParts(date);
  return Object.fromEntries(values.filter(({ type }) => type !== "literal").map(({ type, value }) => [type, Number(value)]));
}

function pacificWallTime(year, month, day, hour) {
  const guess = Date.UTC(year, month - 1, day, hour, 0, 0);
  const atGuess = pacificParts(new Date(guess));
  const displayedAsUtc = Date.UTC(atGuess.year, atGuess.month - 1, atGuess.day, atGuess.hour, 0, 0);
  return new Date(guess - (displayedAsUtc - guess));
}

function nextCheckTime(now = new Date()) {
  const pacific = pacificParts(now);
  for (const hour of [10, 18]) {
    const candidate = pacificWallTime(pacific.year, pacific.month, pacific.day, hour);
    if (candidate.getTime() > now.getTime()) return candidate;
  }
  const tomorrow = new Date(Date.UTC(pacific.year, pacific.month - 1, pacific.day + 1));
  return pacificWallTime(tomorrow.getUTCFullYear(), tomorrow.getUTCMonth() + 1, tomorrow.getUTCDate(), 10);
}

async function scheduleNextCheck() {
  await chrome.alarms.clear(ALARM_NAME);
  const when = nextCheckTime().getTime();
  await chrome.alarms.create(ALARM_NAME, { when });
  await chrome.storage.local.set({ nextCheckAt: new Date(when).toISOString() });
}

async function importCards(cards, searchUrl) {
  if (!Array.isArray(cards) || cards.length === 0) {
    throw new Error("Furnished Finder did not show any property cards yet. Try again after the page finishes loading.");
  }
  const response = await fetch(LOCAL_IMPORT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SFH-Bridge-Version": BRIDGE_VERSION },
    body: JSON.stringify({ cards, search_url: searchUrl })
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || "The local SF Home Finder could not import the cards.");
    error.status = response.status;
    throw error;
  }
  const syncedAt = new Date().toISOString();
  await chrome.storage.local.set({
    lastStatus: `Imported ${payload.added} new and refreshed ${payload.updated || 0} existing supported listings.`,
    lastSyncAt: syncedAt
  });
  return payload;
}

async function importCardsWhenMonitorIsFree(cards, searchUrl) {
  for (let attempt = 0; attempt < IMPORT_RETRY_ATTEMPTS; attempt += 1) {
    try {
      return await importCards(cards, searchUrl);
    } catch (error) {
      if (error.status !== 409 || attempt === IMPORT_RETRY_ATTEMPTS - 1) throw error;
      await chrome.storage.local.set({
        lastStatus: "The regular monitor scan is still running. Furnished Finder will retry shortly."
      });
      await sleep(IMPORT_RETRY_DELAY_MS);
    }
  }
  throw new Error("The local monitor did not become available in time.");
}

async function collectCards(tabId) {
  let response;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      response = await chrome.tabs.sendMessage(tabId, { type: "collectFurnishedFinderCards", loadMore: true });
      if (response?.cards?.length) return response;
    } catch {
      // Furnished Finder renders client-side; a short retry is less brittle than scraping a raw request.
    }
    await sleep(1500);
  }
  return response || { cards: [] };
}

async function scanSearchInBackground(searchUrl) {
  const tab = await chrome.tabs.create({ url: searchUrl, active: false });
  try {
    await sleep(5000);
    const response = await collectCards(tab.id);
    return await importCardsWhenMonitorIsFree(response.cards, searchUrl);
  } finally {
    if (tab.id) await chrome.tabs.remove(tab.id).catch(() => undefined);
  }
}

async function savedSearchUrls() {
  const stored = await chrome.storage.local.get(["searchUrls", "searchUrl"]);
  const urls = Array.isArray(stored.searchUrls) ? stored.searchUrls : [];
  if (stored.searchUrl) urls.unshift(stored.searchUrl);
  return [...new Set(urls.filter((value) => typeof value === "string"))].slice(0, 3);
}

async function sendHeartbeat() {
  const searchUrls = await savedSearchUrls();
  const stored = await chrome.storage.local.get(["nextCheckAt"]);
  const response = await fetch(LOCAL_HEARTBEAT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SFH-Bridge-Version": BRIDGE_VERSION },
    body: JSON.stringify({
      version: BRIDGE_VERSION,
      search_urls: searchUrls,
      next_check_at: stored.nextCheckAt || null
    })
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "The local monitor rejected the bridge heartbeat.");
  }
}

async function runSavedSearches() {
  const searchUrls = await savedSearchUrls();
  if (!searchUrls.length) {
    await chrome.storage.local.set({ lastStatus: "Open a Furnished Finder search and choose ‘Save this search & sync’ once." });
    return;
  }
  let completed = 0;
  const failures = [];
  for (const searchUrl of searchUrls) {
    try {
      await scanSearchInBackground(searchUrl);
      completed += 1;
    } catch (error) {
      failures.push(error.message);
    }
  }
  await chrome.storage.local.set({
    searchUrls,
    lastStatus: failures.length
      ? `${completed} saved search${completed === 1 ? "" : "es"} checked; ${failures.length} failed: ${failures[0]}`
      : `${completed} saved search${completed === 1 ? "" : "es"} checked successfully.`
  });
}

chrome.runtime.onInstalled.addListener(() => { scheduleNextCheck().then(sendHeartbeat).catch(() => undefined); });
chrome.runtime.onStartup.addListener(() => { scheduleNextCheck().then(sendHeartbeat).catch(() => undefined); });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== ALARM_NAME) return;
  try {
    await runSavedSearches();
  } finally {
    await scheduleNextCheck();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "useCurrentFurnishedFinderSearch") return undefined;
  (async () => {
    const searchUrl = String(message.searchUrl || "");
    if (!/^https:\/\/(?:www\.)?furnishedfinder\.com\/housing\//.test(searchUrl)) {
      throw new Error("Open a Furnished Finder housing-results page first.");
    }
    const searchUrls = await savedSearchUrls();
    const updatedSearchUrls = [...new Set([...searchUrls, searchUrl])].slice(-3);
    await chrome.storage.local.set({ searchUrls: updatedSearchUrls });
    const imported = await scanSearchInBackground(searchUrl);
    await scheduleNextCheck();
    await sendHeartbeat();
    sendResponse({ ok: true, ...imported });
  })().catch(async (error) => {
    await chrome.storage.local.set({ lastStatus: `Could not sync: ${error.message}` });
    sendResponse({ ok: false, error: error.message });
  });
  return true;
});
