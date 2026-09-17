// Counts installs by serving the install script itself.
//
// The one-line install fetches a script and pipes it to bash. Served from
// here rather than straight from GitHub, that fetch is a thing this can
// count -- once per address per day, which is as close to "how many people
// installed it" as anything gets without the app reporting on whoever runs
// it. Nothing is added to the app, and the script handed back is byte for
// byte the one in the repository.
//
// The rule this is built around: counting must never be able to break an
// install. Every tally happens after the response is already on its way, and
// if this worker cannot reach GitHub it redirects to GitHub rather than
// failing -- a broken counter costs a number, never somebody's install.
//
// No address is stored. An address is salted with a secret and the current
// date, hashed, and only the hash is kept, so two visits on different days
// produce unrelated keys and nothing here can be turned back into an IP or
// followed across days.

const REPO = "2millerhenry/sf-home-finder";
const SOURCE = `https://raw.githubusercontent.com/${REPO}/HEAD/install.sh`;

// A different route to the same file, for when the one above does not answer.
// github.com resolves HEAD itself and redirects, so it can succeed when the
// raw host is the thing having a bad day -- and curl -fsSL follows it without
// noticing. Sending people back to the URL that just failed would be a
// gesture rather than a fallback.
const FALLBACK = `https://github.com/${REPO}/raw/HEAD/install.sh`;

// Long enough to see a season's trend, short enough that the hashes are gone
// well before anyone could want them. Donations are deliberately not given an
// expiry: a running total that quietly drops everything older than a season is
// not a running total.
const KEEP_SECONDS = 90 * 24 * 60 * 60;

const today = () => new Date().toISOString().slice(0, 10);

/** A stable-for-one-day, unlinkable-across-days key for one address. */
async function fingerprint(ip, date, salt) {
  const source = new TextEncoder().encode(`${ip}:${date}:${salt}`);
  const digest = await crypto.subtle.digest("SHA-256", source);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

// Cloudflare's free tier allows a thousand KV writes a day. One person is
// worth at most this many of them, so a retry loop or a refresh-happy
// afternoon cannot spend the day's budget on a single address and leave real
// installs uncounted.
const MAX_WRITES_PER_ADDRESS = 10;

/** Record one fetch. Never throws: a failed tally is not a failed install. */
async function tally(request, env, source = "script") {
  try {
    const ip = request.headers.get("CF-Connecting-IP");
    if (!ip || !env.COUNTER) return;
    const date = today();

    const key = `u:${date}:${await fingerprint(ip, date, env.SALT ?? "")}`;

    // One key per address per day. The people count is then the number of
    // keys, which no amount of concurrency can distort -- writing the same
    // key twice still leaves one key.
    //
    // Repeat fetches are counted inside that key rather than in a shared
    // counter. A single tally for everybody has to be read and written back,
    // and KV serves reads from a cache, so a burst reads one value and writes
    // one increment: six fetches were measured arriving as two. Per address
    // the race is confined to one person's own rapid retries, and the total
    // across different people stays exact.
    //
    // The count rides in the key's metadata, which comes back from list() --
    // so reading the numbers stays one call per page rather than one per key.
    //
    // KV serves reads from a cache for about a minute, so two fetches from
    // one address inside that window both read the same value and the second
    // is lost: measured, six fetches in a burst recorded as one, and the same
    // six spread past the window recorded as six. The repeat count is
    // therefore a floor. It does not touch the people count, which is the
    // number of keys and needs no read at all.
    const { metadata } = await env.COUNTER.getWithMetadata(key);
    const seen = parseInt(metadata?.n ?? "0", 10) || 0;

    // Past the cap the person is already counted and the repeat number is
    // already a floor, so there is nothing left worth a write.
    if (seen >= MAX_WRITES_PER_ADDRESS) return;

    await env.COUNTER.put(key, "1", {
      expirationTtl: KEEP_SECONDS,
      metadata: { n: seen + 1, p: metadata?.p ?? source },
    });
  } catch {
    // Deliberately silent. Nothing about counting is worth a 500 to somebody
    // who only wants the install script.
  }
}

/** The install script, counted on the way past. */
async function install(request, env, ctx) {
  let upstream;
  try {
    upstream = await fetch(SOURCE, {
      cf: { cacheTtl: 300, cacheEverything: true },
      headers: { "User-Agent": "sf-home-finder-install-counter" },
    });
  } catch {
    upstream = null;
  }

  // If GitHub is unreachable or unhappy, hand the install off rather than
  // failing it. The counter loses one number; the person gets their app.
  if (!upstream || !upstream.ok) {
    return Response.redirect(FALLBACK, 302);
  }

  ctx.waitUntil(tally(request, env));

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/x-shellscript; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

/** The current release's asset for one platform, or null.
 *
 * Asked of GitHub rather than hardcoded, so a new release is picked up
 * without touching this worker. Cached at the edge for five minutes: a
 * release changes a few times a month and this runs on every download.
 */
async function assetUrl(match) {
  try {
    const response = await fetch(
      `https://api.github.com/repos/${REPO}/releases/latest`,
      {
        cf: { cacheTtl: 300, cacheEverything: true },
        headers: {
          Accept: "application/vnd.github+json",
          "User-Agent": "sf-home-finder-install-counter",
        },
      },
    );
    if (!response.ok) return null;
    const payload = await response.json();
    const asset = (payload.assets ?? []).find((a) => String(a.name).includes(match));
    return asset?.browser_download_url ?? null;
  } catch {
    return null;
  }
}

/** A platform zip, counted on the way past.
 *
 * The Mac one-liner was countable because it fetches a script from here. A
 * zip downloaded straight from the releases page never touches this worker,
 * which left every Windows install invisible. Redirecting through here fixes
 * that for anybody following the README; somebody who browses to the releases
 * page directly is still uncounted, exactly as a Mac user who does the same
 * has always been.
 */
async function download(request, env, ctx, match, fallbackName) {
  const url = await assetUrl(match);
  if (!url) {
    // Better the releases page than a dead end.
    return Response.redirect(`https://github.com/${REPO}/releases/latest`, 302);
  }
  ctx.waitUntil(tally(request, env, fallbackName));
  return Response.redirect(url, 302);
}

/** A donation, as Ko-fi reports it the moment it happens.
 *
 * Ko-fi has no endpoint for reading your own history, so this is the only way
 * to get the figure without copying it off their dashboard by hand: they POST
 * here, and what arrives is kept. It follows that the total starts the day the
 * webhook is connected -- anything donated before that lives only in Ko-fi's
 * own records.
 *
 * Only the amount, currency and date are stored. Ko-fi also sends the donor's
 * name, their message and, for shop orders, an email and shipping address;
 * none of it is written down, because a total needs none of it and this
 * project does not keep things it does not need.
 */
async function kofi(request, env) {
  if (request.method !== "POST") return new Response("Not found\n", { status: 404 });

  let payload;
  try {
    const form = await request.formData();
    payload = JSON.parse(form.get("data") ?? "{}");
  } catch {
    // Malformed is not retryable, so accept it and move on rather than
    // leaving Ko-fi redelivering something that will never parse.
    return new Response("ok\n", { status: 200 });
  }

  // Without this anybody who finds the URL can invent donations.
  if (!env.KOFI_TOKEN || payload.verification_token !== env.KOFI_TOKEN) {
    return new Response("Not found\n", { status: 404 });
  }

  const amount = Number.parseFloat(payload.amount);
  if (!Number.isFinite(amount) || amount <= 0 || !env.COUNTER) {
    return new Response("ok\n", { status: 200 });
  }

  const when = String(payload.timestamp ?? "").slice(0, 10) || today();
  // Keyed by Ko-fi's own transaction id, so a redelivery overwrites its
  // earlier copy instead of counting the same donation twice.
  const id = String(payload.message_id ?? payload.kofi_transaction_id ?? crypto.randomUUID());
  await env.COUNTER.put(
    `d:${when}:${id}`,
    "1",
    {
      metadata: {
        a: Math.round(amount * 100),
        c: String(payload.currency ?? "USD").slice(0, 3).toUpperCase(),
        t: String(payload.type ?? "Donation").slice(0, 24),
      },
    },
  );
  return new Response("ok\n", { status: 200 });
}

/** The numbers, for whoever holds the token. */
async function stats(request, env) {
  const offered = (request.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  const expected = env.STATS_TOKEN ?? "";
  if (!expected || offered !== expected) {
    return new Response("Not found\n", { status: 404 });
  }
  if (!env.COUNTER) {
    return Response.json({ days: {}, note: "no KV namespace bound" });
  }

  const days = {};
  let cursor;
  do {
    const page = await env.COUNTER.list({ prefix: "u:", cursor, limit: 1000 });
    for (const { name, metadata } of page.keys) {
      const date = name.split(":")[1];
      days[date] ??= { unique: 0, hits: 0, by: {} };
      days[date].unique += 1;
      days[date].hits += parseInt(metadata?.n ?? "1", 10) || 1;
      const where = metadata?.p ?? "script";
      days[date].by[where] = (days[date].by[where] ?? 0) + 1;
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);

  const donations = { total: {}, count: 0, days: {} };
  cursor = undefined;
  do {
    const page = await env.COUNTER.list({ prefix: "d:", cursor, limit: 1000 });
    for (const { name, metadata } of page.keys) {
      const date = name.split(":")[1];
      const cents = parseInt(metadata?.a ?? "0", 10) || 0;
      const currency = metadata?.c ?? "USD";
      donations.count += 1;
      donations.total[currency] = (donations.total[currency] ?? 0) + cents;
      donations.days[date] ??= { cents: 0, count: 0 };
      donations.days[date].cents += cents;
      donations.days[date].count += 1;
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);

  return Response.json({ asof: today(), days, donations });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (pathname === "/stats") return stats(request, env);
    if (pathname === "/kofi") return kofi(request, env);
    if (pathname === "/windows.zip") return download(request, env, ctx, "Windows", "windows");
    if (pathname === "/mac.zip") return download(request, env, ctx, "macOS", "mac");
    if (pathname === "/install.sh" || pathname === "/") return install(request, env, ctx);
    return Response.redirect(`https://github.com/${REPO}`, 302);
  },
};
