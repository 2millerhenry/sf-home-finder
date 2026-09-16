# Install counter

Serves `install.sh` and counts who fetched it — one per address per day. That
is the closest thing to "how many people installed this" that does not involve
the app reporting on whoever is running it.

Nothing is added to the app, and the script served is byte for byte the one in
the repository. No address is stored: each is salted with a secret and the
current date and hashed, so the stored key cannot be turned back into an
address and the same person produces unrelated keys on different days. Keys
and counters expire after 90 days.

## What it costs

Cloudflare's free tier, with room to spare: 100,000 worker requests a day and
1,000 KV writes a day, against roughly one write per install.

## Deploying it

You need a Cloudflare account. These need your login, so run them yourself.

**1. Sign in.**

```bash
npx wrangler login
```

**2. Make the store and put its id in `wrangler.toml`.**

```bash
npx wrangler kv namespace create COUNTER
```

It prints an `id`. Replace `REPLACE_WITH_KV_ID` in `wrangler.toml` with it.

**3. Set the two secrets.** The salt is what stops a stored hash being matched
back to an address, so use a random one and do not reuse it elsewhere. The
token is what you will use to read the numbers.

The salt is never needed again, so it can go straight in:

```bash
openssl rand -hex 32 | npx wrangler secret put SALT
```

The stats token you do need again, and Cloudflare will not show it a second
time, so this prints it as it sets it:

```bash
STATS=$(openssl rand -hex 32); echo "$STATS" | npx wrangler secret put STATS_TOKEN && echo "stats token: $STATS"
```

Put that token somewhere you keep passwords before moving on.

**4. Deploy.**

```bash
npx wrangler deploy
```

It prints the URL, of the form
`https://sf-home-finder-install.<your-subdomain>.workers.dev`.

**5. Point the README at it**, from the repository root, with that URL and the
token from step 3:

```bash
python3 scripts/use_counter.py https://sf-home-finder-install.<your-subdomain>.workers.dev
```

That rewrites the install command in `README.md` and the comment in
`install.sh`, and prints the two environment variables to set so
`scripts/installs.py` can read the numbers.

## Reading the numbers

```bash
export SF_INSTALL_COUNTER_URL=https://sf-home-finder-install.<your-subdomain>.workers.dev
export SF_INSTALL_COUNTER_TOKEN=<the stats token>
uv run python scripts/installs.py
```

The installs section appears above the downloads. `/stats` returns 404 without
the token, so the numbers are not public.

## If it breaks

A broken counter must never be a broken install, which is what the fallback is
for: if the worker cannot reach GitHub it redirects to GitHub instead, `curl
-fsSL` follows the redirect, and the install proceeds having gone uncounted.

The case that is not covered is the worker itself being down — then the install
command fails, because it now depends on this. The ZIP download on the releases
page never touches the worker, and the old command still works:

```bash
curl -fsSL https://github.com/2millerhenry/sf-home-finder/raw/HEAD/install.sh | bash
```

## Testing changes locally

```bash
npm install
npx wrangler dev --local
curl -H "CF-Connecting-IP: 10.0.0.1" http://127.0.0.1:8787/install.sh
```

`/stats` needs `SALT` and `STATS_TOKEN` in a `[vars]` block in `wrangler.toml`
for local runs. Do not commit real values there — deployed secrets belong in
`wrangler secret put`.

## Donations

Ko-fi has no endpoint for reading your own totals, so it pushes instead: set a
webhook and the worker keeps a running total. It starts from the day you
connect it — anything donated before that stays only in Ko-fi's records.

Only the amount, currency and date are kept. Ko-fi also sends the donor's
name, their message and sometimes an email and shipping address; none of it is
stored.

**1. Get the token.** Open [ko-fi.com/manage/webhooks](https://ko-fi.com/manage/webhooks).
Set the **Webhook URL** to:

```
https://sf-home-finder-install.sfhomefinder.workers.dev/kofi
```

Copy the **Verification Token** shown on that page.

**2. Give it to the worker**, from this folder:

```bash
npx wrangler secret put KOFI_TOKEN
```

Paste the verification token when it asks.

**3. Check it.** Ko-fi's webhook page has a **Send Test** button. After
pressing it, `scripts/installs.py --page` should show the test donation. Delete
it afterwards with the key the store lists:

```bash
npx wrangler kv key list --namespace-id=2ebadfdf07374b13a01d5d1082eba2f8
```

Until `KOFI_TOKEN` is set, `/kofi` returns 404 to everything, so nothing can be
posted to it.
