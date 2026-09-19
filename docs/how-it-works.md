# How it decides, and what it promises

Two things worth understanding if you are relying on this: how a home gets its score, and
what the app does when something goes wrong.

## How a home is scored

- Only criteria you configure participate in the weighted score.
- A configured criterion the listing does not describe gets neutral (50%) credit, not a failure.
- Known out-of-area locations never qualify for the main results, however good the price. They
  stay stored and visible under **Archive**.
- An explicit match gets full credit; an explicit mismatch gets low or zero credit.
- The three strongest supported matches become the displayed reasons. The highest-weight
  explicit mismatch becomes the concern; with none, the most important unknown is shown.
- A configured dealbreaker phrase subtracts 20 points and is reported as the main concern.
- Whole-unit and split shortlists require explicit evidence of an entire home, apply your exact
  per-bedroom caps, and show both total and per-person cost. A known building above your unit
  ceiling stays in the archive; unknown size stays reviewable with a warning.
- What is fetched is kept in SQLite, including results below the display threshold, so changing
  your profile later rescores every home still in play -- the shortlist, Near matches, Starred,
  Passed and every copy of them -- instead of starting over. A home the archive has aged out is not
  rescored when the deal changes until you restore it, which scores it against your deal first;
  rescoring a year of aged-out homes held the dashboard for eleven minutes after an update.
- One home is one row however many sites list it: the same street address and unit, or the same
  building's offer at the same size and rent. Two flats in one building are never joined. A
  second copy is shown under the first only when it disagrees -- a different rent, a date the
  first lacks, or one site saying the home is gone. Starring or passing applies to every copy.
- A home whose size no source states still has a page: the **Other homes** tab, shown when it
  holds anything.
- A listing leaves the shortlist 21 days after it first appeared, unless you starred, noted,
  passed or restored the home. It moves to the **Archive**, marked "Aged out", and Restore brings
  it back for good. A site that starts listing the same home later brings it back as a new
  listing: that is fresh evidence it is still to let.
- An aged-out home is deleted once it has been in the Archive for 120 days and no site has listed
  it for 120 days, unless you starred, noted, passed or restored any copy of it -- those are never
  deleted. "No site lists it" counts only once its site has been searched successfully since it
  was last seen, so a check that could not reach a site (offline, or the site blocking the app)
  deletes nothing that site might still list. A home a site still lists is kept however long ago
  it was found. Without this a year of use held about 210,000 listings.
- Long views come a page at a time, 300 homes a page, and the CSV holds the page on screen.
- A home is shown by the copy a site still lists and that fits your deal. A listing whose page a
  site has handed to a different flat is marked as no longer listed; if you starred it, it stays
  in Saved and says so.
- A home that states no rent is placed on the shortlist by the median rent of similar homes (same
  size, same neighborhood) on your board. That estimate only orders the list: it never decides
  eligibility, never passes a budget, and is never shown or exported.

## What it promises when things break

- Thread and cross-process locks stop scheduled, manual, command-line, and service scans from
  overlapping. The process lock is released by the operating system if a scan dies partway.
- Each platform is isolated: a parser, timeout, or HTTP failure is recorded, logged, and shown
  in the dashboard without stopping later sources.
- The freshness watchdog derives its state from durable source runs. A successful zero-result
  check reads as "Working, no matches", while stale data and repeated failures show one
  recovery action. After two consecutive automatic failures a source pauses briefly.
- HTTP requests use an 8-second timeout, a small connection pool, and bounded detail reads, and a
  check is capped at four minutes (the nightly sweep at fifteen), counted on a clock that keeps
  running while the computer sleeps: a check the lid closed on stops when the computer wakes,
  says the computer slept, and blames no site. Once a source's time is up it is asked for nothing
  more, even by a read the check has already walked away from. Most sites are asked with a user
  agent naming the app; six are sent a desktop browser's instead -- see
  [what the app sends each site](sources.md#what-the-app-sends-each-site).
- The app does not bypass CAPTCHAs, login gates, or bot protections. Public page HTML can change
  at any time; when it does, the adapter fails visibly instead of silently reporting no listings.
- Scans need the computer awake and logged in. Missed scheduled runs are caught up after wake or
  reboot. A Mac asleep still wakes briefly in the background, and a check running then stops at
  its time limit rather than carrying on through the night.
- Whether checking is actually happening is observed, not assumed. The dashboard shows when the
  last check finished and when the next one runs, `/health` reports the scheduler's real state
  under `scheduled_checking`, and Support raises it if the scheduler has stopped or two
  scheduled checks have passed without one completing. All three read the same computation.
- Email is read with an app password over IMAP: messages are opened without being marked as
  read, only known alert senders are searched, message bodies are never stored, and the
  password is written to this machine alone with owner-only permissions. Gmail's OAuth path
  remains for accounts that cannot make app passwords, and needs a Google OAuth client from
  whoever builds the release. Outlook.com is not supported: Microsoft no longer allows app
  passwords for mail.
- Apify and the Chrome bridge are optional, capped, and reported separately.
