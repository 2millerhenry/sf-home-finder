# Where the listings come from

Eighteen sites are read directly, with no account and nothing to set up. Four more only
ever send listings by email, so they need one inbox connected before they can contribute.

Each entry says what is actually read and what is deliberately not, because the difference
matters when a source goes quiet: a parser that breaks is reported, never quietly treated as
"no listings today".

| Platform | Mode | Notes |
|---|---|---|
| Craigslist | Automatic | Public SF rooms/share and apartments searches. Separate room, 0–1 bedroom, broad 2–3 bedroom, and exact 3-bedroom searches each get a bounded detail-page budget. |
| Listings Project | Automatic | The public SF Bay Area collection, filtered to explicit San Francisco rentals and sublets. Each card keeps its direct lister-contact page. |
| Abacus (small buildings) | Automatic | The public San Francisco availability feed, preserving the manager's Apply Now route. Building size stays unknown unless stated. |
| SpareRoom | Automatic | Public SF result cards: price, area, type, and short description. |
| SF Housing Portal | Automatic | The city's own below-market-rate portal (DAHLIA) over its public JSON API. No key, no scraping — one entry per unit type, with real rents and application deadlines. |
| Apartment List | Automatic | Published schema.org data for SF buildings. The search feed carries no bedroom count, so each building is completed from its own page for size, address, amenities and unit count. Where a building publishes no rent per home, its smallest home stands in and the building's starting rent is kept. |
| Zumper | Automatic | Its published schema.org search feed: bedroom count, address, amenities and a real posting date. Most buildings publish no rent on the search page, so a bounded number are enriched from their building page and the rest stay marked unconfirmed. |
| Uloop | Automatic | A university off-campus housing board: rooms in shared flats, sublets and small landlords who post where students look. The only source whose cards all carry a real posting date. One board is read, not five: the San Francisco schools publish the same homes under per-board ids, so the listing's slug is its identity and reading them all would be five times the requests for one board's inventory. |
| RentSFNow | Automatic | The leasing feed of San Francisco's largest landlord: roughly 6,500 apartments across 293 mostly older, rent-controlled buildings, with the neighborhood stated by the landlord rather than inferred. Nothing is on the page and the sitemap is almost all long-gone units, so the search plugin's own JSON endpoint is read instead. Asked for something it has not got, the site answers with recommendations in place of results; those are never stored. |
| AvalonBay | Automatic | One request to its San Francisco page, which ships the whole result set as JSON: unit by unit, with a floor, a square footage, a real move-in date and a rent. Half the buildings on that page are Equity Residential stock AvalonBay markets, which is said rather than implied, and a third of the units are in San Bruno and Pacifica, which are dropped. |
| AppFolio | Automatic | One parser for every small manager who lets through AppFolio, driven by a list in data/appfolio_managers.json so adding a manager is a row rather than code. These are older buildings let by people who post where their own tenants look. A subdomain that is not a tenant site answers 200 with AppFolio's own page-not-found, which is reported as misconfigured rather than as a manager with nothing available. |
| UDR | Automatic | Six San Francisco buildings, each publishing a starting rent per bedroom size rather than per home, so one candidate per building and size the way the city portal files its unit types. A size with nothing free reads '0 Available Apartments' where the rent goes; a rent has to carry a dollar sign to count. |
| Redfin | Automatic | Its published schema.org cards, which pair each building's address, map pin and bedroom range with the rent quoted against that same URL. Where a building lets several sizes, the published rent belongs to its smallest home, so a larger home keeps the building's starting rate as context and its own rent stays unconfirmed. No detail pages are read: a Redfin building page publishes rents for its neighbours and none for the home being viewed. |
| Rent.com | Automatic | Cards to find San Francisco buildings, then each building's own page for the number of homes it holds, a rent per bedroom count, and the earliest published move-in date. Asked for a bedroom count and a rent ceiling together it pads the results with other Bay Area cities, so only the bedroom count is ever requested and the padding it names in its own payload is dropped. |
| ApartmentGuide | Automatic | Rent.com's sister site on the same payload, read a different way: its search pages carry the whole building — a rent per bedroom count, floor plans with their own counts and move-in dates — so no detail page is ever fetched and no building can be stranded by a second request that never lands. Its move-in dates mark the plans that are *not* lettable yet, so a building with homes free today publishes no future date. Past its last page it re-serves the first, which ends the walk. |
| Zillow | Automatic | Read directly, where it used to be a one-time email setup that had delivered nothing. Its search page carries 41 rentals in the page itself, and Zillow serves about 24 pages of one search, so that is what a check reads -- a second apart, and at most once in any fifteen minutes however often Check is pressed. Once a night the sweep asks eight narrower questions instead, one per rent band, to reach homes past that cap. It asks them in the paginated `/homes/for_rent/` form Zillow's robots.txt lists as allowed, not in the `searchQueryState` form the same file disallows, which is what it used until September 2026. Two record shapes have to be read differently: a building, whose sizes and rents are text inside a `units` array, and a single home, whose numbers are numbers. Read as one shape, four homes in every page of 41 lose their bedroom count and 37 lose their rent. One building can also appear twice, as itself and as a unit inside it, both pointing at one page; the id goes in the link so the second does not overwrite the first. |
| Movoto | Automatic | Individual homes rather than buildings: 1,964 San Francisco rentals across forty pages, each with its own rent, unit number and bedroom count, which is why a third of the addresses it brings in belong to no other source. Two payloads sit on the page and only the second is worth reading -- the schema.org blocks carry an address and nothing else, while the page-state script behind them carries the whole record. A home for sale and a home to let are the same record with a different status and a `listPrice` that means a sale price on one and a monthly rent on the other, so every record is checked against that status before it is believed. |
| Trulia | Automatic | Its search payload, one card per building, with the address as separate fields and a rent written as a range. Where a building lets more than one size the bottom of that range belongs to its smallest home, so a larger home keeps it as the building's starting rate and its own rent stays unconfirmed. It refuses unattended requests often, and hard: read too quickly it turns everything away for the best part of an hour, so two pages are read per scan, nothing is retried inside one, and a refusal is reported as rate-limiting rather than breakage. It publishes a placeholder unit number of 32767 where a home has none, which is stripped by value rather than by guessing which numbers are real. |
| HotPads | After one-time setup | Official saved-search emails through the same email connection. |
| Roomies | After one-time setup | Realtime or daily listing-alert emails through the same connection. |
| Facebook Marketplace | After one-time setup | A capped Apify free-tier connector reads ten newest SF Property Rentals cards per scan. No Facebook credentials are used. |
| Furnished Finder | After one-time Chrome setup | A local Chrome bridge reads cards already visible in your own browser session for up to three saved searches. Needs Chrome and the monitor running at check time. |

Sources that cannot run are shown in **Source health** with a direct link to the equivalent
search. They are deliberately not presented as working integrations.

## What the app sends each site

The sources marked Automatic are asked directly, from your computer over your own connection,
with nothing in between. The optional Apify connectors are the exception: those requests go
through Apify, a third party.

- **What it calls itself.** Most sites are asked with a user agent naming the app,
  `SFHousingMonitor/0.1 (local personal-use monitor)`. Six are sent an ordinary desktop Chrome
  user agent instead: Redfin, Trulia, Rent.com and ApartmentGuide, which refuse the app's own
  name (403 from the first two, 429 from the other two, measured side by side), and Zillow and
  Movoto. The pages asked for, and how often, are the same either way.
- **How much it asks.** Measured on the author's install in mid-September 2026, a scheduled check
  sent 135 to 202 requests across every site, and the nightly sweep 229 to 262. Zillow's share
  is 24 pages a check. Its nightly sweep sent about 69, 32 of them following a rent band that
  had run out of pages into the unfiltered search Zillow redirected it to. That is no longer
  followed: the same sweep, run on 18 September, sent 46 requests and read 1,231 listings,
  against 1,219 the night before.
- **How it paces itself.** Zillow's pages go a second apart. Trulia gets two pages a check and
  Redfin six; both are read deeper only by the nightly sweep, at an hour nobody is waiting.
- **When a site says no.** A refusal -- 403, 429, or an empty 202 -- is not retried within the
  check. Two failed checks in a row pause that site's automatic checks for six hours, then
  twelve, then a day; pressing Check still tries it once. A refusal part way through a read keeps
  the pages already read. For Zillow it still counts as a failure, because Zillow blocks by
  address for hours and the pause is what stops the app asking again.
- **robots.txt.** The app does not read robots.txt while it runs. Zillow's was read by hand on
  18 September 2026, and every page the app asks Zillow for is on its allowed list; a test checks
  each one against that copy of the file. The other sites' rules have not been checked this way.
