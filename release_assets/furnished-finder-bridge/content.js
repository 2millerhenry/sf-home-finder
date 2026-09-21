const clean = (value, limit = 1400) => {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1).trim()}…` : text;
};
const FURNISHED_FINDER_HOSTS = new Set(["furnishedfinder.com", "www.furnishedfinder.com"]);
const MAX_CARDS = 180;
let serializedPageData = null;

function cardText(anchor) {
  let node = anchor;
  for (let level = 0; level < 6 && node; level += 1, node = node.parentElement) {
    const text = clean(node.innerText, 1500);
    if (text.length >= 30 && text.length <= 1500) return text;
  }
  return clean(anchor.innerText, 1500);
}

function validSanFranciscoCoordinates(latitude, longitude) {
  return Number.isFinite(latitude) && Number.isFinite(longitude)
    && latitude >= 37.70 && latitude <= 37.84
    && longitude >= -122.54 && longitude <= -122.34;
}

function coordinatesFromAttributes(anchor) {
  for (let level = 0, node = anchor; level < 7 && node; level += 1, node = node.parentElement) {
    const latitude = Number(node.getAttribute("data-latitude") || node.getAttribute("data-lat"));
    const longitude = Number(node.getAttribute("data-longitude") || node.getAttribute("data-lng") || node.getAttribute("data-lon"));
    if (validSanFranciscoCoordinates(latitude, longitude)) return { latitude, longitude };
  }
  return null;
}

function pageData() {
  if (serializedPageData === null) serializedPageData = document.documentElement.innerHTML;
  return serializedPageData;
}

function coordinatesNear(source, start) {
  const slice = source.slice(Math.max(0, start - 800), start + 2_000);
  const latitude = Number(slice.match(/["']?(?:latitude|lat)["']?\s*[:=]\s*(-?\d{1,2}\.\d+)/i)?.[1]);
  const longitude = Number(slice.match(/["']?(?:longitude|lng|lon)["']?\s*[:=]\s*(-?\d{2,3}\.\d+)/i)?.[1]);
  return validSanFranciscoCoordinates(latitude, longitude) ? { latitude, longitude } : null;
}

function coordinatesFromPageData(canonical) {
  const match = canonical.match(/\/property\/(\d+(?:_\d+)?)/);
  if (!match) return null;
  const ids = [match[1], match[1].split("_")[0]];
  const source = pageData();
  for (const id of ids) {
    const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const identifiers = new RegExp(
      `(?:propertyId|property_id|listingId|listing_id|id)["']?\\s*[:=]\\s*["']?${escapedId}(?!\\d)`,
      "gi"
    );
    let identifier;
    while ((identifier = identifiers.exec(source))) {
      const coordinates = coordinatesNear(source, identifier.index);
      if (coordinates) return coordinates;
    }
  }
  return null;
}

function extractCards(cardsByUrl = new Map()) {
  for (const anchor of document.querySelectorAll('a[href*="/property/"]')) {
    let url;
    try {
      url = new URL(anchor.href, window.location.href);
    } catch {
      continue;
    }
    if (!FURNISHED_FINDER_HOSTS.has(url.hostname) || !url.pathname.startsWith("/property/")) continue;
    const canonical = `https://www.furnishedfinder.com${url.pathname.replace(/\/$/, "")}`;
    if (cardsByUrl.has(canonical)) continue;
    const text = cardText(anchor);
    const title = clean(anchor.querySelector("h1, h2, h3, h4")?.innerText || anchor.innerText, 220);
    const price = text.match(/\$\s*[\d,]+\s*(?:\/?\s*month|\/mo)?/i)?.[0] || "";
    const listingType = text.match(/\b(?:Room|Studio|Entire (?:Place|Home)|[12] Bed(?:room)?)\s*-\s*(?:Apartment|House|Condo|Townhouse|Guesthouse|Basement|Other)\b/i)?.[0] || "";
    const coordinates = coordinatesFromAttributes(anchor) || coordinatesFromPageData(canonical);
    if (!title) continue;
    cardsByUrl.set(canonical, {
      url: canonical,
      title,
      price,
      listing_type: listingType,
      summary: text,
      ...(coordinates || {})
    });
    if (cardsByUrl.size >= MAX_CARDS) break;
  }
  return [...cardsByUrl.values()];
}

const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function collectCards(loadMore) {
  const cardsByUrl = new Map();
  const collectCurrentPage = async () => {
    serializedPageData = null;
    extractCards(cardsByUrl);
    if (!loadMore || cardsByUrl.size >= MAX_CARDS) return;

    // The result grid lazy-loads as a person scrolls. Accumulate cards rather
    // than only reading the final DOM, because some result grids virtualize old rows.
    const originalY = window.scrollY;
    let unchangedPasses = 0;
    for (let pass = 0; pass < 14 && cardsByUrl.size < MAX_CARDS && unchangedPasses < 3; pass += 1) {
      const before = cardsByUrl.size;
      const priorHeight = document.documentElement.scrollHeight;
      window.scrollTo(0, priorHeight);
      await pause(750);
      extractCards(cardsByUrl);
      const hasChanged = cardsByUrl.size > before || document.documentElement.scrollHeight > priorHeight;
      unchangedPasses = hasChanged ? 0 : unchangedPasses + 1;
    }
    window.scrollTo(0, originalY);
  };

  const pageFingerprint = () => [...document.querySelectorAll('a[href*="/property/"]')]
    .map((anchor) => anchor.getAttribute("href") || "")
    .filter(Boolean)
    .slice(0, 5)
    .join("|");
  const nextPageButton = () => [...document.querySelectorAll("button")].find((button) =>
    clean(button.getAttribute("aria-label") || button.innerText).toLowerCase() === "next page" && !button.disabled
  );
  const waitForNextPage = async (before) => {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await pause(700);
      if (pageFingerprint() && pageFingerprint() !== before) return true;
    }
    return false;
  };

  await collectCurrentPage();
  // Results are paginated today, not merely lazy-loaded. This runs only in a
  // disposable background tab, so it never moves the page the person is reading.
  for (let page = 0; loadMore && page < 4 && cardsByUrl.size < MAX_CARDS; page += 1) {
    const next = nextPageButton();
    if (!next) break;
    const before = pageFingerprint();
    next.click();
    if (!await waitForNextPage(before)) break;
    window.scrollTo(0, 0);
    serializedPageData = null;
    await collectCurrentPage();
  }
  return extractCards(cardsByUrl);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "collectFurnishedFinderCards") return undefined;
  collectCards(Boolean(message.loadMore))
    .then((cards) => sendResponse({ cards, searchUrl: window.location.href }))
    .catch((error) => sendResponse({ cards: [], error: String(error?.message || error) }));
  return true;
});
