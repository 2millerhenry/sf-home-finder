from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from datetime import UTC, date, datetime
from dataclasses import replace
from html import unescape
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .apify import ApifyTokenStore
from .classification import ROOM, UNKNOWN, WHOLE_UNIT
from .deal_profile import SF_NEIGHBORHOODS
from .connectors import gmail_provider_key
from .coverage import looks_blocked
from .freshness import PACIFIC
from .gmail_alerts import AlertEmail, GmailAlertMailbox
from .location import (
    declared_outside_sf_area_hint,
    declared_outside_sf_url_hint,
    outside_sf_location_label,
    sf_area_from_address,
)
from .models import ListingCandidate
from .preferences import Preferences, setting_int


LOGGER = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


class ScanTimeUpError(SourceError):
    """The check's time for this source ran out before a request was sent.

    Raised by the scanner's own clients, never by a site. A SourceError so
    that every source which keeps the pages it has read when a later one is
    refused keeps them now as well; the scanner records it as the check
    running out of time, never as the site failing (see Scanner._within_ceiling).
    """


class PartialReadError(SourceError):
    """A read that stopped part way, carrying the listings it had already read.

    Most paginated sources keep what they have and report success when a later
    page is refused (the PARTIAL_WALK note): a refusal on page four is one page
    being rate-limited, and calling the source failed is what kept Redfin dark
    for nine days. Zillow cannot be read that way. It blocks by address, and the
    block outlasts six hours, so a refusal part way through has to reach the
    backoff -- which only ever hears a failure. The two needs pulled against
    each other and the backoff won: a 503 on page four of Zillow's search on 15
    September threw away the three pages already read, and a timeout on page
    five on 12 September threw away four.

    This carries both. The scanner stores ``listings`` exactly as it would a
    finished read, then records the run as the failure ``cause`` describes, so
    the homes are kept and repeated refusals still pause the source.
    """

    def __init__(self, cause: Exception, listings: list[ListingCandidate]):
        super().__init__(str(cause))
        self.cause = cause
        self.listings = listings


# These are source-integrity signals, not rent preferences.  A public search
# card can be live while its detail page has been removed or is part of a
# repeated malformed-post campaign, so the detail page is the authority before
# a Craigslist whole unit reaches the shortlist.
_CRAIGSLIST_REMOVED_PATTERN = re.compile(
    r"posting has (?:been )?(?:flagged for removal|expired|deleted|removed)",
    re.IGNORECASE,
)
_CRAIGSLIST_BEDROOM_PATTERN = re.compile(
    r"\b(?:(?P<studio>studio)|(?P<number>[0-3])\s*(?:bd|br|bed(?:room)?s?)|"
    r"(?P<word>one|two|three)[- ]bed(?:room)?s?)\b",
    re.IGNORECASE,
)
_CRAIGSLIST_SUSPECT_PROFILE_PATTERN = re.compile(
    r"\b(?:mark\s+robart|license\s+(?:jane|jessica))\s+properties\b",
    re.IGNORECASE,
)
_CRAIGSLIST_OFF_MARKET_LOCATION_PATTERN = re.compile(
    r"\b(?:chicago|rogers\s+park|metra|morse\s+station|henry\s+cowell|"
    r"roaring\s+camp)\b",
    re.IGNORECASE,
)


def _craigslist_bedroom_count(text: str | None) -> int | None:
    """Read one explicit advertised bedroom count without guessing from prose."""
    if not text:
        return None
    match = _CRAIGSLIST_BEDROOM_PATTERN.search(text)
    if not match:
        return None
    if match.group("studio"):
        return 0
    if match.group("number") is not None:
        return int(match.group("number"))
    return {"one": 1, "two": 2, "three": 3}.get(str(match.group("word")).casefold())


def _clean_text(value: object, limit: int | None = None) -> str:
    # Structured data legitimately publishes booleans and numbers where earlier
    # pages published strings -- Apartment List sends petsAllowed as "Yes" on
    # some buildings and as true on others. A source must not lose its whole
    # detail fetch to that. Falsy values keep their long-standing empty result.
    if not isinstance(value, str):
        value = "" if not value else str(value)
    text = re.sub(r"\s+", " ", value).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _parse_price(value: str | None, *, require_currency: bool = False) -> int | None:
    if not value:
        return None
    # Card text can begin with a street number. Prefer an explicitly dollar-marked
    # amount before falling back to a source's already-price-only field.
    match = re.search(r"\$\s*([0-9][0-9,]*)", value)
    if not match and not require_currency:
        match = re.search(r"\$?\s*([0-9][0-9,]*)", value)
    return int(match.group(1).replace(",", "")) if match else None


def _zumper_source_id(url: str) -> str:
    """Zumper's own listing id, not the words after it.

    A Zumper listing lives at ``/listings/29225041p/1-bedroom-soma-san-francisco-ca``,
    and the last segment is the same for every one-bedroom Zumper lists in SoMa.
    Keyed on that slug, each such listing overwrote the last one stored under it
    -- 13 of 90 Zumper rows on a real board were keyed this way -- and a starred
    home could be rewritten into a different flat. The numbered segment before
    it is the listing's own (``p12936`` on a building page).
    """
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"listings", "apartment-buildings"} and re.fullmatch(r"p?\d+p?", parts[1]):
        return parts[1]
    return _source_id(url)


def _source_id(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    # HotPads listing URLs end in the shared literal `/pad`; use the full
    # canonical URL in that one case so distinct listings never collide.
    if slug == "pad":
        return hashlib.sha256(url.encode()).hexdigest()[:20]
    return slug or hashlib.sha256(url.encode()).hexdigest()[:20]


# Facebook's Property Rentals cards often contain only a latitude/longitude in
# `locationText`.  These are deliberately small, supplied-search bounding boxes
# rather than a broad attempt to reverse-geocode all of San Francisco.  A point
# outside them remains unknown (or outside), which is safer than inventing an
# attractive neighborhood from a city-wide pin.
_FACEBOOK_NEIGHBORHOOD_BOXES: tuple[tuple[str, float, float, float, float], ...] = (
    ("Duboce Triangle", 37.761558, 37.772550, -122.438848, -122.424128),
    ("Mission Dolores", 37.752995, 37.774980, -122.440882, -122.411442),
    # A verified public Potrero result used this pin:
    # 37.75780, -122.40093. This conservative box covers Potrero Hill without
    # swallowing the Mission Dolores search area above.
    ("Potrero Hill", 37.747500, 37.770500, -122.419000, -122.384000),
    ("Bernal Heights", 37.729124, 37.751115, -122.429802, -122.400363),
    ("Noe Valley", 37.736680, 37.758669, -122.447998, -122.418559),
    ("Marina", 37.797000, 37.810000, -122.455000, -122.421000),
)

# These labels are only accepted when the seller actually writes the place name
# in the card.  They make broad city-level cards more useful without pretending
# that a nearby landmark reveals an exact address.
_VISIBLE_SF_AREA_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Mission Dolores", ("mission dolores", "dolores park")),
    ("Duboce Triangle", ("duboce triangle",)),
    ("Potrero Hill", ("potrero hill",)),
    ("Noe Valley", ("noe valley",)),
    ("Bernal Heights", ("bernal heights",)),
    ("Dolores Heights", ("dolores heights",)),
    ("Mission District", ("mission district", "the mission")),
    ("Haight-Ashbury", ("haight ashbury", "haight-ashbury")),
    ("Hayes Valley", ("hayes valley",)),
    ("Lower Haight", ("lower haight",)),
    ("NOPA", ("north of panhandle", "nopa")),
    ("Panhandle", ("the panhandle", "panhandle home", "panhandle house")),
    ("Eureka Valley", ("eureka valley",)),
    ("Castro", ("the castro", "castro with", "castro room", "castro district")),
    ("Cole Valley", ("cole valley",)),
    ("Inner Richmond", ("inner richmond",)),
    ("Dogpatch", ("dogpatch",)),
    ("Mission Bay", ("mission bay",)),
    ("SoMa", ("south of market", "soma", "fidi")),
    ("Financial District", ("financial district",)),
    ("North Beach", ("north beach",)),
    ("Marina", ("marina district", "marina room", "in the marina")),
    ("Diamond Heights", ("diamond heights",)),
    ("Sunnyside", ("sunnyside",)),
    ("Outer Sunset", ("outer sunset",)),
    ("Parkside", ("parkside",)),
    ("Bayview", ("bayview",)),
    ("Excelsior", ("excelsior",)),
    ("Portola", ("portola district",)),
    ("Menlo Park (outside SF)", ("menlo park",)),
    ("Near Golden Gate Park", ("golden gate park",)),
    ("Near Ocean Beach", ("ocean beach",)),
    ("Near Lands End", ("lands end",)),
)


def visible_sf_area_hint(text: str | None) -> str | None:
    """Return a location phrase explicitly visible in a public card's text.

    This is deliberately not a geocoder: an unlabelled city-wide map pin stays
    unknown, because assigning a desirable neighborhood from it would be a
    guess.  Landmark labels are clearly marked ``Near …`` for the same reason.
    """
    normalized = _normal_text(text or "")
    for area, phrases in _VISIBLE_SF_AREA_HINTS:
        for phrase in phrases:
            pattern = rf"(?<!\w){re.escape(_normal_text(phrase))}(?!\w)"
            if re.search(pattern, normalized):
                return area
    return None


def declared_sf_area_hint(text: str | None) -> str | None:
    """Return an area only when prose explicitly describes the home's location."""
    normalized = _normal_text(text or "")
    location_prefix = r"(?:located|situated|available(?:\s+for\s+rent)?|for\s+rent)\s+(?:in|at)\s+"
    for area, phrases in _VISIBLE_SF_AREA_HINTS:
        for phrase in phrases:
            value = re.escape(_normal_text(phrase))
            if re.search(
                rf"{location_prefix}(?:san\s+francisco\s+)?(?:the\s+)?[^.!?]{{0,32}}(?<!\w){value}(?!\w)",
                normalized,
            ):
                return area
            if re.search(rf"(?<!\w){value}(?!\w)(?:\s*/\s*[a-z ]+)?\s+(?:district|neighbou?rhood)\b", normalized):
                return area
    return None


# San Francisco ZIP codes that sit essentially inside one of the canonical
# neighbourhoods. Deliberately partial: a ZIP that straddles two areas (94110
# covers both the Mission and Bernal Heights, 94114 the Castro and Noe Valley)
# is left out, because a wrong neighbourhood costs more than an unknown one in a
# score where area carries the most weight.
_UNAMBIGUOUS_SF_ZIPS = {
    "94104": "Financial District",
    "94108": "Chinatown",
    "94111": "Embarcadero",
    "94123": "Marina",
    "94129": "Presidio Heights",
    "94130": "Treasure Island",
    "94132": "Park Merced",
    "94158": "Mission Bay",
}


def sf_area_from_zip(value: object) -> str | None:
    """Return a neighbourhood only where the ZIP does not straddle two."""
    digits = re.sub(r"\D", "", str(value or ""))[:5]
    area = _UNAMBIGUOUS_SF_ZIPS.get(digits)
    return area if area in SF_NEIGHBORHOODS else None


def sf_area_from_slug(url: str | None) -> str | None:
    """Recover a neighborhood that a listing URL names in its path.

    Zumper builds its slugs as building-name, neighborhood, city, state, so the
    area is right there even though the structured data only ever says "San
    Francisco". Matching against the canonical list rather than guessing which
    trailing words are the neighborhood keeps one-word areas (SoMa) and
    multi-word ones (Potrero Hill) equally correct, and the longest match wins
    so "Mission Bay" is never read as "Mission".
    """
    path = _normal_text(str(url or "").replace("-", " ").replace("/", " "))
    if not path:
        return None
    best: str | None = None
    for area in SF_NEIGHBORHOODS:
        needle = _normal_text(area)
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", path):
            if best is None or len(needle) > len(_normal_text(best)):
                best = area
    return best


def sf_target_coordinate_neighborhood(latitude: object, longitude: object) -> str | None:
    """Return a target area only when a source map pin is unambiguous."""
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    for name, south, north, west, east in _FACEBOOK_NEIGHBORHOOD_BOXES:
        if south <= latitude <= north and west <= longitude <= east:
            return name
    return None


def facebook_coordinate_neighborhood(location: str | None) -> str | None:
    """Return a target area when Facebook's coordinate-only pin is unambiguous."""
    match = re.search(r"\b(37\.\d{4,})\s+(-122\.\d{4,})\b", location or "")
    if not match:
        return None
    return sf_target_coordinate_neighborhood(match.group(1), match.group(2))


# Whether a source's search looks everywhere that source keeps its San
# Francisco homes.
#
# This is the fact the app's rule about absence rests on, and it is stated
# once per source rather than assumed, because assuming it is how a home
# somebody could still rent quietly leaves the shortlist. A search that reads
# a filtered slice, or stops at a wall the site puts in front of it, returns
# without homes it never asked about; treating those homes as gone is the one
# mistake this app has decided it will never make. So the default is False --
# it is read with ``getattr(source, "search_covers_inventory", False)``, and a
# source that has not thought about the question gets the answer that costs
# nothing -- and a source may only set it True with the reason written beside
# it. See ``database._SOURCE_LAST_SEARCHED`` for what it buys.
#
# A source that walks pages sets it True on the class and lowers it on itself
# for a run that stopped early: reaching the last page of a twelve-page
# ceiling with homes still arriving is not the end of the board, and a refusal
# part way through a walk is not the end of it either.
class ListingSource(Protocol):
    platform: str
    mode: str
    search_url: str
    manual_reason: str | None
    detail_budget: int
    # Optional; see the note above this class. Absent means False.
    search_covers_inventory: bool

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]: ...

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate: ...


def card_proves_listed(source: object) -> bool:
    """Is a home coming back in this source's search evidence it is still let?

    True for all but one, and the default is True because for a normal source
    a card is its own current answer about a home -- re-reading the page it
    has just handed us would spend the recheck allowance confirming what we
    were told a second ago.

    Zillow is the exception, and not a small one. Its search kept returning
    502 Precita Ave and 161 Albion St as ``FOR_RENT`` cards on the days their
    own pages read "Off market", and three of eight of its active homes
    sampled from a real board were off the market -- roughly 790 of that
    board's 2,103. Absence can never catch those, because the search does not
    stop returning them, so its homes are re-read whether the last search
    brought them back or not.
    """
    return bool(getattr(source, "search_card_proves_listed", True))


def covers_inventory(source: object) -> bool:
    """Did this source's search look everywhere it keeps its San Francisco homes?

    The default lives here, in one place, so that adding a source is never
    also a decision about absence: a new source says nothing and is believed
    about nothing.
    """
    return bool(getattr(source, "search_covers_inventory", False))


class GmailHousingAlertSource:
    """Turn first-party saved-search email cards into ordinary listing candidates."""

    mode = "setup"
    provider = "gmail"
    connector_key = "gmail"
    detail_budget = 0
    opaque_alert_message: str | None = None

    def __init__(
        self,
        platform: str,
        search_url: str,
        manual_reason: str,
        query: str,
        hostname: str,
        url_path_pattern: str,
        mailbox: GmailAlertMailbox,
        canonical_hostname: str | None = None,
    ):
        self.platform = platform
        self.search_url = search_url
        self._manual_reason = manual_reason
        self.query = query
        self.hostname = hostname
        self.url_path_pattern = url_path_pattern
        self.mailbox = mailbox
        self.canonical_hostname = canonical_hostname or f"www.{hostname}"
        self.empty_result_message: str | None = None
        self.last_alert_count = 0
        self.connector_state_key = gmail_provider_key(platform)

    @property
    def mode(self) -> str:
        return "automatic" if self.mailbox.is_connected else "setup"

    @property
    def manual_reason(self) -> str:
        if self.mailbox.is_connected:
            return ""
        return self._manual_reason

    def _direct_url(self, href: str) -> str | None:
        decoded = unescape(href)
        candidates = [decoded]
        for _ in range(3):
            decoded = unquote(decoded)
            candidates.append(decoded)
        if self.hostname == "facebook.com":
            pattern = re.compile(
                r"https?://(?:www\.)?facebook\.com/marketplace/item/(\d+)", re.IGNORECASE
            )
            for candidate in candidates:
                match = pattern.search(candidate)
                if match:
                    return f"https://www.facebook.com/marketplace/item/{match.group(1)}/"
            return None
        pattern = re.compile(
            rf"https?://(?:www\.)?{re.escape(self.hostname)}"
            rf"(?P<path>{self.url_path_pattern}[^\s\"'<>?&]*)",
            re.IGNORECASE,
        )
        for candidate in candidates:
            match = pattern.search(candidate)
            if match:
                path = match.group("path").rstrip(".,)")
                return f"https://{self.canonical_hostname}{path}"
        return None

    def _listing_source_id(self, original_url: str, title: str, card_text: str) -> str:
        """Provide a stable identity even when an alert provider wraps a card link."""
        return _source_id(original_url)

    def _is_listing_card(
        self, original_url: str, title: str, card_text: str, subject: str, anchor: object | None = None
    ) -> bool:
        """Reject navigation and preference links mixed into alert email cards."""
        return True

    def _listing_neighborhood(
        self, card_text: str, subject: str, original_url: str, title: str, preferences: Preferences
    ) -> str | None:
        return self._neighborhood(card_text, preferences)

    @staticmethod
    def _card_text(anchor) -> str:
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            value = _clean_text(node.get_text(" ", strip=True), 520)
            if 35 <= len(value) <= 520:
                return value
        return _clean_text(anchor.get_text(" ", strip=True), 520)

    @staticmethod
    def _neighborhood(text: str, preferences: Preferences) -> str | None:
        normalized = _normal_text(text)
        aliases = preferences.mapping_value("neighborhood_aliases")
        for name in (
            preferences.list_value("ideal_neighborhoods")
            + preferences.list_value("preferred_neighborhoods")
            + preferences.list_value("acceptable_neighborhoods")
        ):
            if any(_normal_text(phrase) in normalized for phrase in [name, *aliases.get(name, [])]):
                return name
        return None

    def _from_email(self, email: AlertEmail, preferences: Preferences) -> list[ListingCandidate]:
        soup = BeautifulSoup(email.html, "html.parser")
        anchors = soup.select("a[href]")
        if not anchors:
            # Zillow and Facebook generally use HTML cards. A text-only email can
            # still be useful when it contains a direct listing URL.
            anchors = []
            for url in re.findall(r"https?://[^\s<>]+", f"{email.text} {email.html}"):
                parsed = self._direct_url(url)
                if parsed:
                    anchors.append((parsed, email.subject, email.text or email.subject))

        listings: list[ListingCandidate] = []
        seen_listing_ids: set[str] = set()
        for anchor in anchors:
            if isinstance(anchor, tuple):
                original_url, title, card_text = anchor
            else:
                original_url = self._direct_url(str(anchor.get("href", "")))
                if not original_url:
                    continue
                title = _clean_text(anchor.get_text(" ", strip=True)) or email.subject
                card_text = self._card_text(anchor) or email.subject
            source_id = self._listing_source_id(original_url, title, card_text)
            if not self._is_listing_card(original_url, title, card_text, email.subject, anchor):
                continue
            if source_id in seen_listing_ids:
                continue
            seen_listing_ids.add(source_id)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=source_id,
                    title=_clean_text(title, 180) or f"{self.platform} alert",
                    original_url=original_url,
                    price=_parse_price(card_text, require_currency=True),
                    neighborhood=self._listing_neighborhood(
                        card_text, email.subject, original_url, title, preferences
                    ),
                    listing_type=None,
                    summary=_clean_text(card_text, 420),
                    metadata={"alert_message_id": email.message_id, "alert_subject": email.subject},
                )
            )
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search_query(preferences, self.query)

    def search_for_trigger(
        self,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
    ) -> list[ListingCandidate]:
        query = self.query
        if trigger == "initial_discovery":
            query = re.sub(r"\bnewer_than:\d+d\b", "newer_than:5d", query)
        return self._search_query(preferences, query)

    def _search_query(self, preferences: Preferences, query: str) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        seen_listing_ids: set[str] = set()
        self.empty_result_message = None
        self.last_alert_count = 0
        emails = self.mailbox.messages(query, max_results=50)
        # Mail from the provider is not the same thing as an alert from the
        # provider. A single "Welcome to Zillow" signup message counted as an
        # alert that yielded no listings, which reported a parser failure and
        # told the reader their notification settings needed attention -- when
        # all that had happened was that they had not saved a search yet.
        alerts = [email for email in emails if self._is_alert_email(email)]
        self.last_alert_count = len(alerts)
        for email in alerts:
            for listing in self._from_email(email, preferences):
                if listing.source_id not in seen_listing_ids:
                    listings.append(listing)
                    seen_listing_ids.add(listing.source_id)
        if alerts and not listings:
            if self.opaque_alert_message:
                self.empty_result_message = self.opaque_alert_message
                return []
            raise SourceError(
                f"{self.platform} alert emails were found, but none contained a direct listing link. "
                "The email format or notification settings may need attention."
            )
        return listings

    # Sign-up, verification, receipt and account mail all arrive from the same
    # address as the alerts. None of it is evidence about whether alerts work,
    # so it is not counted as one.
    _NOT_AN_ALERT = re.compile(
        r"\b(?:welcome|verify|verification|confirm your|activate|password|"
        r"receipt|invoice|payment|billing|sign(?:ed)?[- ]in|log(?:ged)?[- ]in|"
        r"security alert|two[- ]factor|terms|privacy policy|survey|"
        r"tour (?:request|confirmed)|application (?:received|update)|"
        r"getting started|get started|complete your|finish (?:setting|your))\b",
        re.IGNORECASE,
    )

    def _is_alert_email(self, email: Any) -> bool:
        subject = str(getattr(email, "subject", "") or "")
        return not self._NOT_AN_ALERT.search(subject)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        # The saved-search email is the source of data. We deliberately do not
        # fetch Zillow/Facebook pages here; doing so would reintroduce bot blocks.
        return listing


def _normal_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


class ZillowAlertSource(GmailHousingAlertSource):
    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Zillow",
            search_url="https://www.zillow.com/myzillow/savedsearches/",
            manual_reason="Set up Zillow Instant saved-search email alerts, then connect Gmail from Alerts.",
            # Zillow's current rental alerts use listing-address subjects such
            # as "606 Capp St #105 just listed", not the older generic
            # "saved search" / "new listings" wording. The card-link parser
            # below remains the admission gate, so ordinary Zillow mail stays
            # out even with this wider mailbox query.
            query="from:(zillow.com) newer_than:90d",
            hostname="zillow.com",
            url_path_pattern=r"/(?:homedetails|b)/",
            mailbox=mailbox,
        )

    def _direct_url(self, href: str) -> str | None:
        direct = super()._direct_url(href)
        if direct:
            return direct

        # USC's Proofpoint mail gateway wraps Zillow's click-tracking card
        # links. Keep that protected URL intact: opening it in the user's
        # browser follows the normal security check and Zillow redirect. We do
        # not resolve it during a scan, which avoids generating tracking opens
        # or crawling protected Zillow pages.
        decoded = unescape(href)
        if (
            "urldefense.com/" in decoded.casefold()
            and "click.mail.zillow.com/" in decoded.casefold()
        ):
            return decoded
        return None

    def _listing_source_id(self, original_url: str, title: str, card_text: str) -> str:
        if "click.mail.zillow.com/" in original_url.casefold():
            # The protection wrapper is unique to each email delivery. Build a
            # repeatable ID from the card's own visible address copy, ignoring price
            # so an advertised rent correction updates the same home.
            identity = re.sub(r"\$\s*[0-9][0-9,]*(?:\s*/\s*(?:mo|month))?", "", title)
            return "zillow-email-" + hashlib.sha256(_normal_text(identity).encode()).hexdigest()[:20]
        return super()._listing_source_id(original_url, title, card_text)

    @staticmethod
    def _subject_address(subject: str) -> str | None:
        match = re.match(r"\s*(?P<address>.+?)\s+just\s+listed\b", subject, re.IGNORECASE)
        if not match:
            match = re.match(r"\s*new\s+for\s+rent:\s*(?P<address>.+?)\s*(?:,\s*san\s+francisco.*)?$", subject, re.IGNORECASE)
        return _normal_text(match.group("address")) if match else None

    def _is_listing_card(
        self, original_url: str, title: str, card_text: str, subject: str, anchor: object | None = None
    ) -> bool:
        if "click.mail.zillow.com/" not in original_url.casefold():
            return True
        # A parent node can contain an entire alert email, so judge protected
        # card links from their own visible label. Zillow places the price and
        # bedroom/room detail on the address link, while status and footer
        # links have neither.
        text = title
        has_listing_facts = bool(
            _parse_price(text, require_currency=True) is not None
            and re.search(r"\b(?:[0-9]+\s*(?:bd|br)|studio|bed(?:room)?|private\s+room|room\s+for\s+rent)\b", text, re.IGNORECASE)
        )
        subject_address = self._subject_address(subject)
        matches_subject_address = bool(subject_address and subject_address in _normal_text(title))
        previous_anchor = anchor.find_previous("a") if hasattr(anchor, "find_previous") else None
        previous_label = (
            _normal_text(previous_anchor.get_text(" ", strip=True))
            if previous_anchor is not None and hasattr(previous_anchor, "get_text")
            else ""
        )
        # Zillow places a small "For rent New" link immediately before each
        # actual saved-search card. Similar cards below it are recommendations,
        # so never treat them as matching results simply because they share the
        # same protected link format.
        marked_new = "for rent" in previous_label and "new" in previous_label
        return has_listing_facts and (matches_subject_address or marked_new)

    def _listing_neighborhood(
        self, card_text: str, subject: str, original_url: str, title: str, preferences: Preferences
    ) -> str | None:
        neighborhood = super()._listing_neighborhood(
            card_text, subject, original_url, title, preferences
        )
        if neighborhood or "click.mail.zillow.com/" not in original_url.casefold():
            return neighborhood
        return self._neighborhood(subject, preferences)


class HotPadsAlertSource(GmailHousingAlertSource):
    """Import first-party HotPads saved-search alert cards through Gmail."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="HotPads",
            search_url="https://hotpads.com/san-francisco-ca/apartments-for-rent",
            manual_reason="Save a HotPads search with email alerts, then connect Gmail from Alerts.",
            # HotPads permits the alert cadence to be selected by the account
            # owner. Filtering by sender and then requiring a direct /pad link
            # keeps normal product mail out of the monitor.
            query="from:(hotpads.com) newer_than:90d",
            hostname="hotpads.com",
            url_path_pattern=r"/[^/\s\"'<>?&]+/pad",
            mailbox=mailbox,
            canonical_hostname="hotpads.com",
        )


class RoomiesAlertSource(GmailHousingAlertSource):
    """Import Roomies listing alerts without scraping its protected search page."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Roomies",
            search_url="https://www.roomies.com/rooms/san-francisco-ca",
            manual_reason="Set Roomies Listing Alerts to Realtime or Daily, then connect Gmail from Alerts.",
            # Individual public Roomies listings use /rooms/<numeric-id>.
            # This intentionally rejects location and filter pages such as
            # /rooms/san-francisco-ca.
            query="from:(roomies.com) newer_than:90d",
            hostname="roomies.com",
            url_path_pattern=r"/rooms/[0-9]+",
            mailbox=mailbox,
        )


class ZumperAlertSource(GmailHousingAlertSource):
    """Import Zumper's own saved-search emails without scraping its protected site."""

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Zumper",
            search_url="https://www.zumper.com/apartments-for-rent/san-francisco-ca",
            manual_reason="Save Zumper searches with email alerts, then connect Gmail from Alerts.",
            # Zumper's individual listings use /address/ or /apartment-buildings/.
            # Excluding broad /apartments-for-rent/ routes keeps alert-management
            # and search-result links out of the shortlist.
            query="from:(zumper.com) newer_than:90d",
            hostname="zumper.com",
            url_path_pattern=r"/(?:address|apartment-buildings)/",
            mailbox=mailbox,
        )


class ApartmentsComAlertSource(GmailHousingAlertSource):
    """Import Apartments.com saved-search links with their stable listing ID."""

    opaque_alert_message = (
        "No direct listing links were found. Apartments.com may be sending opaque tracking links; "
        "forward its original alert email to this Gmail inbox unchanged."
    )

    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Apartments.com",
            search_url="https://www.apartments.com/san-francisco-ca/",
            manual_reason="Save Apartments.com searches with email alerts, then connect Gmail from Alerts.",
            # Individual Apartments.com homes use a descriptive path followed by
            # a stable short listing ID, unlike city and filter result pages.
            query="from:(apartments.com) newer_than:90d",
            hostname="apartments.com",
            url_path_pattern=r"/[^/]+/[a-z0-9]{7,}",
            mailbox=mailbox,
        )


class FacebookMarketplaceAlertSource(GmailHousingAlertSource):
    def __init__(self, mailbox: GmailAlertMailbox):
        super().__init__(
            platform="Facebook Marketplace",
            search_url="https://www.facebook.com/marketplace/you/alerts/",
            manual_reason=(
                "Turn on Marketplace saved-search email notifications, then connect Gmail from Alerts. "
                "The source activates only if the alerts contain direct listing links."
            ),
            query="from:(facebookmail.com) Marketplace newer_than:90d",
            hostname="facebook.com",
            url_path_pattern=r"/marketplace/item/",
            mailbox=mailbox,
        )


class ListingsProjectSource:
    """Read the small, public SF feed from Listings Project.

    The source page is a curated weekly collection rather than a syndication
    network.  We retain only cards that explicitly say San Francisco and link
    to Listings Project's own individual listing page, so Oakland/Bay Area
    cards and generic collection links never leak into the monitor.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Listings Project"
    # The public SF collection, read whole in one request. Small enough that
    # the page is the inventory.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://www.listingsproject.com/real-estate/san-francisco-bay-area"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "The current Bay Area issue has no explicit San Francisco rentals in supported categories."

    _residential_categories = (
        "rooms for rent",
        "rooms for sublet",
        "apartments for rent",
        "apartments for sublet",
        "houses for rent",
        "houses for sublet",
        "homes for rent",
        "homes for sublet",
        "condos for rent",
        "condos for sublet",
    )

    @staticmethod
    def _card_for(anchor):
        parent = anchor.parent
        while parent is not None:
            classes = set(parent.get("class", [])) if getattr(parent, "get", None) else set()
            if parent.name == "div" and {"flex", "flex-col", "mb-8"}.issubset(classes):
                return parent
            parent = parent.parent
        return None

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        listings: list[ListingCandidate] = []
        seen_urls: set[str] = set()
        for anchor in soup.select('h4 a[href^="/listings/"]'):
            card = self._card_for(anchor)
            if card is None:
                continue
            card_text = _clean_text(card.get_text(" ", strip=True), 1200)
            # The SF Bay Area collection includes East Bay cards.  City level is
            # written on every card; accept only an explicit SF address instead
            # of guessing from a regional collection.
            if "san francisco" not in _normal_text(card_text):
                continue
            category_node = card.select_one("div.text-grey-dark.mb-2.text-smish")
            category_text = _clean_text(category_node.get_text(" ", strip=True)) if category_node else ""
            category = _clean_text(category_text.rsplit("|", 1)[-1]) if "|" in category_text else ""
            if not category:
                category_match = re.search(
                    r"\b(?:rooms|apartments|houses|homes|condos)\s+for\s+(?:rent|sublet)\b",
                    card_text,
                    re.IGNORECASE,
                )
                category = _clean_text(category_match.group(0)) if category_match else ""
            if not any(name in _normal_text(category) for name in self._residential_categories):
                continue
            original_url = urljoin(self.search_url, str(anchor.get("href", "")))
            if original_url in seen_urls:
                continue
            seen_urls.add(original_url)
            title = _clean_text(anchor.get_text(" ", strip=True), 180)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=title or "Listings Project rental",
                    original_url=original_url,
                    price=_parse_price(card_text, require_currency=True),
                    neighborhood=visible_sf_area_hint(card_text),
                    listing_type=category or None,
                    summary=card_text,
                    # The source's own listing page contains the lister's
                    # contact details, so it is a higher-value first-contact
                    # route without storing anyone's contact data locally.
                    metadata={"direct_lister": True, "source_collection": "SF Bay Area"},
                )
            )
        if not listings and soup.select('h4 a[href^="/listings/"]'):
            return []
        if not listings and not soup.select('h4 a[href^="/listings/"]'):
            raise SourceError("Listings Project returned no recognizable listing cards; its page format may have changed.")
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class AbacusSource:
    """Read a local small-property manager's public AppFolio availability feed.

    Abacus publishes its own direct application links.  The business describes
    its portfolio as single homes, condos, and small multi-unit buildings, but
    a card without an explicit unit count remains *unknown* so the dashboard's
    <=50-unit rule stays intact.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Abacus (small buildings)"
    # The manager's own availability page, read whole in one request. Its
    # vacancies are the page; there is nothing behind it to page through.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://abacus.appfolio.com/listings"
    manual_reason = None
    detail_budget = 10
    empty_result_message = "Abacus currently reports no available rental properties."

    def not_needed_for(self, preferences: Preferences) -> str | None:
        """Why this deal has no use for Abacus, or None when it has.

        See SpareRoomSource.not_needed_for, whose mirror image this is. Said
        here rather than written over ``empty_result_message`` as it was: that
        was never put back, so once a deal had been rooms-only, Abacus really
        having nothing to let was reported as a skip.
        """
        if set(preferences.deal_profile.enabled_paths) & set(_WHOLE_UNIT_BEDROOMS):
            return None
        return "Whole homes are not in Your deal, and Abacus lets nothing else, so it was not asked."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(".js-listing-item")
        if not cards:
            no_inventory = soup.select_one(".listings__no-vacancies:not(.u-hidden)")
            if no_inventory and "no available properties" in _normal_text(no_inventory.get_text(" ", strip=True)):
                return []
            raise SourceError("Abacus returned no recognizable availability cards; its page format may have changed.")
        listings: list[ListingCandidate] = []
        for card in cards:
            address = _clean_text(
                (card.select_one(".js-listing-address") or card).get_text(" ", strip=True), 220
            )
            if "san francisco" not in _normal_text(address):
                continue
            title_anchor = card.select_one(".js-listing-title a[href]")
            if title_anchor is None:
                continue
            original_url = urljoin(self.search_url, str(title_anchor.get("href", "")))
            facts = _clean_text(card.get_text(" ", strip=True), 1000)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=_clean_text(title_anchor.get_text(" ", strip=True), 180) or address,
                    original_url=original_url,
                    price=_parse_price(facts, require_currency=True),
                    # A street address is not a neighborhood.  Leave it unknown
                    # unless the manager itself writes a target area.
                    neighborhood=visible_sf_area_hint(facts),
                    listing_type=_clean_text(
                        (card.select_one(".js-listing-blurb-bed-bath") or card).get_text(" ", strip=True),
                        80,
                    ),
                    summary=facts,
                    metadata={"address": address, "small_manager": True},
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        response = client.get(listing.original_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = _clean_text((soup.select_one(".js-show-title") or soup.title).get_text(" ", strip=True), 180)
        summary = _clean_text(
            " ".join(
                item.get_text(" ", strip=True)
                for item in soup.select(".header__summary, .description, .js-show-description, .list__item")
            ),
            1800,
        )
        apply_anchor = soup.select_one("a.js-apply-now[href]")
        metadata = dict(listing.metadata)
        if apply_anchor is not None:
            metadata["application_url"] = urljoin(listing.original_url, str(apply_anchor.get("href", "")))
        return replace(
            listing,
            title=title or listing.title,
            price=_parse_price(summary, require_currency=True) or listing.price,
            summary=summary or listing.summary,
            listing_type=(
                _clean_text((soup.select_one(".header__summary") or "").get_text(" ", strip=True), 120)
                or listing.listing_type
            ),
            neighborhood=listing.neighborhood or visible_sf_area_hint(summary),
            metadata=metadata,
        )


def _pets_allowed(value: object) -> bool | None:
    """schema.org petsAllowed arrives as a boolean or as Yes/No text."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"yes", "true"}:
        return True
    if text in {"no", "false"}:
        return False
    return None


def _json_ld_blocks(document: str) -> list[dict]:
    """Every schema.org object on a page, flattened out of its script tags."""
    soup = BeautifulSoup(document, "html.parser")
    blocks: list[dict] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(node.string or node.get_text() or "")
        except (ValueError, json.JSONDecodeError):
            continue
        for entry in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(entry, dict):
                blocks.append(entry)
    return blocks


def _schema_types(node: dict) -> set[str]:
    raw = node.get("@type")
    return {str(value) for value in (raw if isinstance(raw, list) else [raw]) if value}


# A price written as digits and nothing else. Redfin quotes its rents as
# strings ("3395"), which read as no price at all until they are accepted, and
# a loose match here would turn a floor area of "348-400" into a $348 rent.
_NUMERIC_PRICE = re.compile(r"^\s*\$?\s*([\d,]+)(?:\.\d+)?\s*$")


def _offer_price(node: object, key: str = "lowPrice") -> int | None:
    if not isinstance(node, dict):
        return None
    for candidate in (key, "price", "lowPrice"):
        value = node.get(candidate)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(round(value))
        if isinstance(value, str):
            match = _NUMERIC_PRICE.match(value)
            if match:
                price = int(match.group(1).replace(",", ""))
                if price > 0:
                    return price
    return None


def today_in_san_francisco(now: datetime | None = None) -> date:
    """What day it is where these homes are.

    Takes the moment rather than only reading the clock, because a rule about
    the last day of something cannot be shown to hold on that last day unless
    the clock can be held still.
    """
    return (now or datetime.now(UTC)).astimezone(PACIFIC).date()


def application_deadline_passed(value: object, *, today: date | None = None) -> bool:
    """Is the day an application had to be in by already behind us?

    The city's portal states a deadline as a bare day at midnight with a UTC
    offset -- "2026-09-19T00:00:00.000+0000" -- for a window that really
    closes at 5pm Pacific on the day it names. So the day as written *is* the
    deadline and must be read as written: turning the whole stamp into Pacific
    would move it back to the evening before and close every lottery a full
    day early.

    Only the other side of the comparison has to be worked out in the portal's
    own timezone. Read in UTC, it is already tomorrow from 5pm Pacific
    onwards, which is exactly the hour somebody filing on their last day would
    be told the lottery was over.

    Everything unreadable answers no. An open waitlist publishes no deadline
    at all, and a date this cannot parse is a date this does not know about;
    in both cases the home may well still be real, and the answer here decides
    whether anybody is shown it.
    """
    stated = str(value or "").strip()
    if not stated:
        return False
    try:
        deadline = date.fromisoformat(stated[:10])
    except ValueError:
        return False
    return deadline < (today if today is not None else today_in_san_francisco())


class SFHousingPortalSource:
    """Read the City of San Francisco's own below-market-rate rental portal.

    DAHLIA is public government data served as plain JSON, so unlike every
    other source here it needs no key, conflicts with no site's terms, and
    cannot quietly start refusing unattended requests. Each record is a
    building offering several unit types, so a building with a studio and a
    one-bedroom becomes two candidates the deal profile can judge separately.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "SF Housing Portal"
    # One request returns the city's whole below-market-rate list, unfiltered
    # and unpaginated, so a home missing from it is a home the city has taken
    # down.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://housing.sfgov.org/listings"
    api_url = "https://housing.sfgov.org/api/v1/listings.json"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "The city portal currently lists no open below-market rentals."

    # DAHLIA's own unit vocabulary, mapped to the metadata hint that
    # classification.py already understands. SRO is a single room, not a home.
    UNIT_TYPES = {
        "studio": ("studio", WHOLE_UNIT),
        "sro": (None, ROOM),
        "1 br": ("1 bedroom", WHOLE_UNIT),
        "2 br": ("2 bedroom", WHOLE_UNIT),
        "3 br": ("3 bedroom", WHOLE_UNIT),
        "4 br": ("4 bedroom", WHOLE_UNIT),
    }

    @staticmethod
    def _timestamp(value: object) -> str | None:
        """Normalise Salesforce's +0000 offset to something fromisoformat accepts."""
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("+0000"):
            text = f"{text[:-5]}+00:00"
        return text

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        response = client.get(self.api_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceError("The SF housing portal returned a non-JSON response.") from exc
        records = payload.get("listings") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise SourceError(
                "The SF housing portal response did not contain a listings array; its API may have changed."
            )
        listings: list[ListingCandidate] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            # Resale and new-sale rows are ownership opportunities, not rentals.
            if "rental" not in str(record.get("Tenure") or "").casefold():
                continue
            if _normal_text(record.get("Building_City")) != "san francisco":
                continue
            listings.extend(self._candidates(record))
        return listings

    def _candidates(self, record: dict) -> list[ListingCandidate]:
        listing_id = _clean_text(str(record.get("Id") or record.get("listingID") or ""), 60)
        name = _clean_text(record.get("Name"), 180)
        if not listing_id or not name:
            return []
        address = _clean_text(record.get("Building_Street_Address"), 220)
        modified = self._timestamp(record.get("LastModifiedDate"))
        due = self._timestamp(record.get("Application_Due_Date"))
        if application_deadline_passed(due):
            # The lottery is over. The portal goes on publishing a listing for
            # months after its applications closed, and a below-market rent
            # nobody can apply for is the most convincing ghost a shortlist can
            # carry -- cheap, real-looking, and in the city's own words.
            # Refused here, where the deadline is read, so no unit type of a
            # closed listing ever becomes a candidate.
            return []
        units_available = record.get("Units_Available")
        reserved_for = _clean_text(record.get("Reserved_community_type"), 60)
        summaries = record.get("unitSummaries")
        if not isinstance(summaries, dict):
            return []

        # A building can publish the same unit type in both the general and the
        # income-reserved bucket. Keep the lowest rent offered for each type
        # rather than emitting two near-identical rows.
        cheapest: dict[str, tuple[str, float, object]] = {}
        for bucket in ("general", "reserved"):
            for unit in summaries.get(bucket) or []:
                if not isinstance(unit, dict):
                    continue
                raw_type = _clean_text(unit.get("unitType"), 40)
                rent = unit.get("minMonthlyRent")
                if not raw_type or not isinstance(rent, (int, float)) or rent <= 0:
                    continue
                key = raw_type.casefold()
                if key not in cheapest or rent < cheapest[key][1]:
                    cheapest[key] = (raw_type, float(rent), unit.get("totalUnits"))

        listings: list[ListingCandidate] = []
        for key, (raw_type, rent, total_units) in cheapest.items():
            unit_hint, housing_kind = self.UNIT_TYPES.get(key, (None, UNKNOWN))
            # The portal gives one page per building, but each unit type is a
            # different home at a different rent. Without a distinct URL the
            # repository's canonical-URL dedup treats them as one listing and
            # the cheaper studio disappears behind the two-bedroom. The portal
            # ignores the extra parameter and serves the same page.
            unit_slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unit"
            detail = [f"Below-market-rate {raw_type} through the San Francisco housing portal."]
            if address:
                detail.append(f"Building at {address}.")
            if isinstance(total_units, (int, float)) and total_units:
                detail.append(f"{int(total_units)} {raw_type} units in this listing.")
            if due:
                detail.append(f"Applications due {due[:10]}.")
            if reserved_for:
                detail.append(f"Reserved for {reserved_for.casefold()} applicants.")
            metadata: dict[str, object] = {
                "address": address,
                "below_market_rate": True,
                "application_due_date": due,
                # Said out loud, rather than left for silence to imply. The
                # end-of-scan sweep marks a home gone once its stated deadline
                # passes, and stored metadata outlives the card it came from,
                # so a lottery whose deadline the city later extended would
                # keep that mark for ever and a real, open, below-market home
                # would stay hidden. The portal publishing it with a deadline
                # that has not passed is the portal saying applications are
                # open, which is the one thing that can undo the mark.
                "verified_inactive": False,
                "units_available": units_available,
                "sf_portal_unit_type": raw_type,
            }
            if unit_hint:
                metadata["unit_type"] = unit_hint
            if modified:
                metadata["listing_timestamp"] = modified
            if reserved_for:
                metadata["reserved_community_type"] = reserved_for
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=f"{listing_id}:{key}",
                    title=f"{name} - {raw_type}",
                    original_url=f"{self.search_url}/{listing_id}?unit={unit_slug}",
                    price=int(round(rent)),
                    # The portal states a street address and a ZIP but never a
                    # neighbourhood. Take a name the portal itself uses first,
                    # then the block the address sits on, and only then the ZIP,
                    # which can answer for the few that do not straddle two
                    # areas.
                    neighborhood=(
                        visible_sf_area_hint(f"{name} {address}")
                        or sf_area_from_address(address)
                        or sf_area_from_zip(record.get("Building_Zip_Code"))
                    ),
                    listing_type=raw_type,
                    summary=_clean_text(" ".join(detail), 1000),
                    metadata=metadata,
                    housing_kind=housing_kind,
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class ApartmentListSource:
    """Read Apartment List's published schema.org data for San Francisco.

    The search feed is deliberately thin: a name, a link, an image and a price
    range, with no bedroom count and no address. On its own that is not enough
    to place a building in any of the three shortlists, so each result is
    completed from its own page, which publishes one ``Apartment`` block per
    unit type along with the building's address, amenities, unit count and a
    last-modified date.

    A building is represented by its cheapest available unit, because that is
    the one that decides whether the building is worth opening at all. The
    other unit types are named in the summary so nothing is hidden.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Apartment List"
    # No claim. The structured data carries no filter, but it carries one
    # page: 66 successful runs on a real board returned 19 or 20 buildings and
    # never another number. San Francisco has more than twenty. What is not in
    # the page is not absent, only unread.
    search_covers_inventory = False
    mode = "automatic"
    search_url = "https://www.apartmentlist.com/ca/san-francisco"
    manual_reason = None
    # The search feed cannot classify a building on its own, so these are worth
    # completing generously; the scanner still stops at its own deadline.
    detail_budget = 20
    empty_result_message = "Apartment List published no San Francisco buildings in its structured data."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        # Through _require_page, as the paginated sources are, so a refusal is
        # said as one. Left to raise_for_status a 202 with an empty body parsed
        # as "no structured building data; its page format may have changed",
        # sending somebody to a parser that was fine, and a 503 reached the
        # reader as an HTTPStatusError with a link to MDN.
        document = _require_page(client.get(self.search_url), self.platform)
        products = [block for block in _json_ld_blocks(document) if "Product" in _schema_types(block)]
        if not products:
            raise _read_nothing(self.platform, document, "structured building data")
        listings: list[ListingCandidate] = []
        for product in products:
            name = _clean_text(product.get("name"), 180)
            url = str(product.get("url") or product.get("@id") or "").strip()
            if not name or not url.startswith("https://www.apartmentlist.com/"):
                continue
            offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
            price = _offer_price(offers)
            high = offers.get("highPrice") if isinstance(offers.get("highPrice"), (int, float)) else None
            detail = [f"{name} on Apartment List."]
            if price and high and high > price:
                detail.append(f"Published rents run from ${price:,} to ${int(round(high)):,} a month.")
            elif price:
                detail.append(f"Published rent from ${price:,} a month.")
            detail.append("Home size is unconfirmed until this building's own page is read.")
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(url),
                    title=name,
                    original_url=url,
                    price=price,
                    neighborhood=visible_sf_area_hint(name),
                    listing_type="Apartment building",
                    summary=_clean_text(" ".join(detail), 700),
                    metadata={
                        "building_listing": True,
                        "price_low": price,
                        "price_high": int(round(high)) if high else None,
                    },
                    # Apartment List rents whole apartments, never a room in
                    # someone's home, so the workflow is known even when the
                    # number of bedrooms is not.
                    housing_kind=WHOLE_UNIT,
                )
            )
        return listings

    @staticmethod
    def _representative_unit(blocks: list[dict]) -> tuple[dict, int | None] | None:
        """The unit that decides whether this building is worth opening.

        A priced home wins, cheapest first. Smaller buildings often publish
        their unit mix with no rents at all; rather than leave those
        unclassified and invisible, the smallest home stands in and the
        building's own starting rent is kept.
        """
        units = [block for block in blocks if "Apartment" in _schema_types(block)]
        priced = [
            (unit, price)
            for unit, price in ((unit, _offer_price(unit.get("offers"))) for unit in units)
            if price is not None
        ]
        if priced:
            return min(priced, key=lambda pair: pair[1])
        sized = [unit for unit in units if isinstance(unit.get("numberOfBedrooms"), (int, float))]
        if sized:
            return min(sized, key=lambda unit: unit["numberOfBedrooms"]), None
        return None

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        """Complete a building from its own page: size, address, date, amenities."""
        # A refusal is never read as the building's page. An empty 202 passed
        # raise_for_status, parsed into a thinner record of this building than
        # the card it replaced, and the recheck pass then stamped the home
        # confirmed on the strength of a page that said nothing.
        document = _require_page(client.get(listing.original_url), self.platform)
        blocks = _json_ld_blocks(document)
        complexes = [b for b in blocks if "ApartmentComplex" in _schema_types(b)]
        building = complexes[0] if complexes else {}

        metadata = dict(listing.metadata)
        detail = [f"{listing.title} on Apartment List."]

        address = building.get("address") if isinstance(building.get("address"), dict) else {}
        street = _clean_text(address.get("streetAddress"), 160)
        if street:
            metadata["address"] = street
            detail.append(f"Address: {street}.")

        # The <=50-unit rule needs a real count; an unknown one stays unknown.
        units = building.get("numberOfAvailableAccommodationUnits")
        building_units = None
        if isinstance(units, dict) and isinstance(units.get("value"), (int, float)):
            building_units = int(units["value"])
        elif isinstance(units, (int, float)):
            building_units = int(units)

        representative = self._representative_unit(blocks)
        price = listing.price
        unit_label = ""
        if representative is not None:
            unit, unit_price = representative
            price = unit_price or listing.price
            bedrooms = unit.get("numberOfBedrooms")
            if isinstance(bedrooms, (int, float)):
                metadata["bedrooms"] = int(bedrooms)
                unit_label = "studio" if int(bedrooms) == 0 else f"{int(bedrooms)}-bedroom"
            baths = unit.get("numberOfBathroomsTotal")
            if isinstance(baths, (int, float)) and not isinstance(baths, bool):
                metadata["bathrooms"] = float(baths)
            floor = unit.get("floorSize")
            if isinstance(floor, dict) and isinstance(floor.get("value"), (int, float)):
                metadata["floor_size_sqft"] = int(floor["value"])
            if unit_price and unit_label:
                detail.append(f"Cheapest available home is a {unit_label} from ${unit_price:,} a month.")
            elif unit_price:
                detail.append(f"Cheapest available home is from ${unit_price:,} a month.")
            elif unit_label:
                detail.append(
                    f"Smallest home listed is a {unit_label}; this building publishes no rent per home."
                )

        other = sorted(
            {
                _clean_text(block.get("name"), 60).split(" - ")[-1]
                for block in blocks
                if "Apartment" in _schema_types(block) and block.get("name")
            }
        )
        if len(other) > 1:
            detail.append("This building also lists: " + ", ".join(other) + ".")

        amenities = [
            _clean_text(feature.get("name"), 48)
            for feature in building.get("amenityFeature") or []
            if isinstance(feature, dict) and feature.get("name")
        ]
        if amenities:
            detail.append("Amenities: " + ", ".join(amenities[:12]) + ".")
        if _pets_allowed(building.get("petsAllowed")) is True:
            detail.append("Pets allowed.")

        for block in blocks:
            if block.get("@type") == "WebPage" and block.get("dateModified"):
                metadata["listing_timestamp"] = _clean_text(block["dateModified"], 40)
                break

        return replace(
            listing,
            price=price or listing.price,
            neighborhood=(
                listing.neighborhood
                or sf_target_coordinate_neighborhood(
                    (building.get("geo") or {}).get("latitude"),
                    (building.get("geo") or {}).get("longitude"),
                )
                or sf_area_from_slug(listing.original_url)
                or visible_sf_area_hint(f"{listing.title} {street}")
            ),
            listing_type=f"{unit_label.capitalize()} apartment" if unit_label else listing.listing_type,
            summary=_clean_text(" ".join(detail), 1200),
            building_units=building_units,
            metadata=metadata,
        )


class ZumperSource:
    """Read the schema.org search feed Zumper publishes for San Francisco.

    Zumper was previously listed as blocked, but it answers ordinary requests
    and publishes a full ``SearchResultsPage`` with a bedroom count, address,
    amenities and — unusually among the free sources — a real ``datePosted``.

    What it does not publish on the search page is rent: only individual-unit
    listings carry an offer, while apartment buildings load their price after
    the page renders. Those buildings are enriched one at a time within the
    scanner's detail budget, and until that happens their rent stays unknown
    rather than being guessed at.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Zumper"
    # No claim. The feed is city-scoped and unfiltered, which is why this
    # once said otherwise -- but one request is one page of it. Across 71
    # successful runs on a real board it returned 49 or 50 homes and never
    # another number, which is a page size, not a city. Until the walk is
    # written, a Zumper home missing from a run is a home this source did not
    # ask about.
    search_covers_inventory = False
    mode = "automatic"
    search_url = "https://www.zumper.com/apartments-for-rent/san-francisco-ca"
    manual_reason = None
    detail_budget = 10
    empty_result_message = "Zumper published no San Francisco results in its structured data."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        # A refusal said as one, not as a page whose format changed: see
        # ApartmentListSource.search.
        document = _require_page(client.get(self.search_url), self.platform)
        entries: list[dict] = []
        for block in _json_ld_blocks(document):
            if block.get("@type") != "SearchResultsPage":
                continue
            main = block.get("mainEntity")
            if isinstance(main, dict):
                entries = [e for e in main.get("itemListElement") or [] if isinstance(e, dict)]
            break
        if not entries:
            raise _read_nothing(self.platform, document, "structured search results")

        listings: list[ListingCandidate] = []
        for entry in entries:
            item = entry.get("item")
            if not isinstance(item, dict) or item.get("@type") != "RealEstateListing":
                continue
            url = str(item.get("url") or item.get("@id") or "").strip()
            name = _clean_text(item.get("name"), 180)
            if not name or not url.startswith("https://www.zumper.com/"):
                continue
            about = item.get("about") if isinstance(item.get("about"), dict) else {}
            address = about.get("address") if isinstance(about.get("address"), dict) else {}
            locality = _normal_text(address.get("addressLocality"))
            # The feed is city-scoped, but a neighbouring city slipping in would
            # quietly break the whole point of the search.
            if locality and locality != "san francisco":
                continue

            street = _clean_text(address.get("streetAddress"), 160)
            amenities = [
                _clean_text(feature.get("name"), 48)
                for feature in about.get("amenityFeature") or []
                if isinstance(feature, dict) and feature.get("value") and feature.get("name")
            ]
            bedrooms = about.get("numberOfBedrooms")
            pets = _pets_allowed(about.get("petsAllowed"))

            detail = [f"{name} listed on Zumper."]
            if street:
                detail.append(f"Address: {street}.")
            if amenities:
                # Amenities are scored from the listing text, so they belong in
                # the summary rather than in a metadata key nothing reads.
                detail.append("Amenities: " + ", ".join(amenities[:12]) + ".")
            if pets is True:
                detail.append("Pets allowed.")
            elif pets is False:
                detail.append("No pets.")

            price = _offer_price(item.get("offers"))
            if price is None:
                detail.append("Rent is not published in the search feed and is still unconfirmed.")

            metadata: dict[str, object] = {"address": street, "zumper_amenities": amenities}
            baths = about.get("numberOfBathroomsTotal")
            if isinstance(baths, (int, float)) and not isinstance(baths, bool):
                metadata["bathrooms"] = float(baths)
            if isinstance(bedrooms, (int, float, str)) and str(bedrooms).strip() != "":
                metadata["bedrooms"] = bedrooms
            posted = _clean_text(item.get("datePosted"), 40)
            if posted:
                metadata["listing_timestamp"] = posted

            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_zumper_source_id(url),
                    title=name,
                    original_url=url,
                    price=price,
                    neighborhood=sf_area_from_slug(url) or visible_sf_area_hint(f"{name} {street}"),
                    listing_type="Apartment rental",
                    summary=_clean_text(" ".join(detail), 1200),
                    metadata=metadata,
                    housing_kind=WHOLE_UNIT,
                )
            )
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        """Fetch the building page only to recover a rent the search feed omitted."""
        if listing.price:
            return listing
        document = _require_page(client.get(listing.original_url), self.platform)
        price = None
        for block in _json_ld_blocks(document):
            price = price or _offer_price(block.get("offers"))
        if price is None:
            return listing
        metadata = dict(listing.metadata)
        metadata["zumper_price_source"] = "building page"
        summary = listing.summary.replace(
            " Rent is not published in the search feed and is still unconfirmed.", ""
        )
        return replace(
            listing,
            price=price,
            summary=_clean_text(f"{summary} Rents from ${price:,} a month.", 1200),
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Redfin and Rent.com
#
# Two of the few large portals that answer an unattended request, and the only
# ones publishing whole San Francisco buildings as structured data. They share
# two failure modes, so the guards below are written once.
# ---------------------------------------------------------------------------


# Whole homes only. Neither lets a room in somebody's flat, so the private-room
# path has no bearing on what is worth asking them for.
_WHOLE_UNIT_BEDROOMS = {
    "studio": 0,
    "one_bedroom": 1,
    "two_bedroom": 2,
    "three_bedroom": 3,
    "four_bedroom": 4,
}


# The scanner identifies itself honestly, and Craigslist, Zumper, Apartment
# List and the city portal all answer it. Four sources do not: measured side by
# side on one request each, the monitor's own User-Agent gets 403 from Redfin
# and Trulia and 429 from Rent.com and ApartmentGuide, while a browser string
# gets 200 from all four. The filter is reading the name, not the behaviour --
# the request rate, the pages asked for and what is done with them are
# identical either way -- so this borrows a browser's name to get past it and
# changes nothing else. Remove these lines and those four sources simply stop
# working; nothing else breaks.
#
# Accept-Language earns its place separately. Trulia alone refuses a request
# that does not state one, with the same 403 it gives the honest User-Agent:
# browser string without this header is 403, with it is 200. The other three
# are indifferent to it, so it is set here once rather than special-cased.
# What this app calls itself when it asks. Kept here beside the browser string
# because which one a source is read with is a property of the reading, and
# having a second copy of it in the scanner meant a check written to verify the
# sources could quietly ask a different question than the app does: Zumper
# answers this honestly and serves the browser string a bot challenge, so a
# checker holding its own copy reported a working source as broken.
# The order the free sources are shown in, which is not the order they are read
# in. Scan order is chosen for speed and politeness and reads on the page as a
# jumble; this leads with the names somebody recognises before asking them to
# believe the rest, then keeps like with like -- the portals, the rental
# marketplaces, the landlords who publish their own buildings, and the local
# boards. Anything not named here is appended alphabetically rather than
# dropped, so adding a source can never silently remove it from the page.
SHOWCASE_ORDER = (
    # The ones a renter has already heard of.
    "Zillow",
    "Trulia",
    "Redfin",
    "Craigslist",
    "Movoto",
    "ApartmentGuide",
    # Rental marketplaces.
    "Zumper",
    "Rent.com",
    "Apartment List",
    # Landlords publishing their own buildings.
    "AvalonBay",
    "UDR",
    "AppFolio",
    "RentSFNow",
    "Abacus (small buildings)",
    # Local and community boards.
    "SF Housing Portal",
    "SpareRoom",
    "Uloop",
    "Listings Project",
)


def showcase_sorted(platforms: Iterable[str]) -> list[str]:
    """Order source names for the page, keeping any stranger at the end."""
    known = {name: index for index, name in enumerate(SHOWCASE_ORDER)}
    return sorted(platforms, key=lambda name: (known.get(name, len(known)), name))


MONITOR_HEADERS = {
    "User-Agent": "SFHousingMonitor/0.1 (local personal-use monitor)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# The nightly run that reads a rate-limited source to the bottom. Named here
# rather than imported from the scanner because sources must not depend on it.
DEEP_SWEEP_TRIGGER = "deep_sweep"


# Whose rate limiter sees a source's requests. A site is not a hostname: one
# company answers for several of them and counts every request against the same
# address. Two sources in the same group are therefore never read at the same
# moment. Zillow Group fronts Zillow, Trulia and HotPads; CoStar fronts
# Rent.com, ApartmentGuide and Apartments.com -- and on the evening this was
# written all of those refused inside a minute of each other (503 from Zillow,
# 403 from Trulia, 429 from Rent.com and ApartmentGuide) while every
# independent site answered normally, which is what one limiter looks like.
_TRAFFIC_GROUPS = {
    "zillow.com": "zillow-group",
    "trulia.com": "zillow-group",
    "hotpads.com": "zillow-group",
    "rent.com": "costar",
    "apartmentguide.com": "costar",
    "apartments.com": "costar",
}


def traffic_group(source: object) -> str:
    """Whose limiter answers for this source.

    The registrable domain by default, which is right for every site that
    answers only for itself and keeps two tenants of one host together --
    appfolio.com serves two property managers here, facebook.com two sources.
    The map above names the companies that front several domains. Two labels
    is enough for every site read here; a .co.uk would need more.
    """
    declared = getattr(source, "traffic_group", None)
    if declared:
        return str(declared)
    host = (urlsplit(str(getattr(source, "search_url", "") or "")).hostname or "").lower()
    if not host:
        # Nothing is fetched for it, so it shares a limiter with nobody.
        return f"source:{getattr(source, 'platform', type(source).__name__)}"
    labels = host.split(".")
    domain = ".".join(labels[-2:]) if len(labels) > 1 else host
    return _TRAFFIC_GROUPS.get(domain, domain)


def _pages_for_trigger(source: object, trigger: str) -> int:
    """How many pages this source reads on this kind of run.

    Most sources here are read in full every time, because doing so costs
    seconds. Trulia and Redfin cannot be: they answer 403 and 202 once they
    have had enough, and a source that has been turned away returns nothing at
    all, which is worse than a shallow source that works. So they are read
    shallowly on the runs somebody is waiting for and to the bottom once a day
    at an hour nobody is.
    """
    shallow = int(getattr(source, "max_pages", 1))
    if trigger != DEEP_SWEEP_TRIGGER:
        return shallow
    return max(shallow, int(getattr(source, "deep_max_pages", shallow)))


# Why a mid-walk refusal is not a failed source (the PARTIAL_WALK note).
#
# A paginated source is read page by page, and a refusal on page four is the
# site rate-limiting one page rather than turning this caller away. Redfin made
# the cost of getting this wrong measurable: every 202 it ever returned came on
# page 3, 4 or 7 and never once on page 1, so raising there threw away the three
# pages of San Francisco homes already in hand, recorded the source as failed,
# and twice pushed it into a 24-hour backoff -- nine days delivering nothing
# while its first page answered HTTP 200 throughout.
#
# So each walk keeps what it has read and stops, rather than losing it -- after
# a refusal, and after a later page that times out or drops the connection,
# which costs the pages in hand just the same. A failure on the *first* page
# still raises, because a source that really is blocked has to be reported as
# blocked rather than as a quiet zero.

def _stated_bathrooms(*records: object) -> float | None:
    """A bathroom count one of these records states, or None.

    Rent.com and ApartmentGuide both publish ``bathCount`` on a floor plan,
    and Redfin publishes ``numBaths``. None of the three was read, so every
    one of their homes showed "Baths n/a" while the number sat in the payload
    the parser had already opened -- 73 of the 439 homes on the owner's
    shortlist, and on one search page alone ApartmentGuide stated 491 of
    them.

    Zero is a plan that states no bathroom rather than a home without one,
    and a number past a dozen belongs to some other field, so both are left
    unknown -- the same bar ``bathrooms_from_listing`` applies to the text.
    """
    for record in records:
        if not isinstance(record, dict):
            continue
        # Rent.com and ApartmentGuide write the count on each floor plan
        # rather than on the building. They say the same thing about the
        # home only when the plans agree; a building letting one-bath and
        # two-bath homes states neither about the one being shown.
        plans = record.get("floorPlans")
        if isinstance(plans, list) and plans:
            counts = {
                float(plan["bathCount"])
                for plan in plans
                if isinstance(plan, dict)
                and isinstance(plan.get("bathCount"), (int, float))
                and not isinstance(plan.get("bathCount"), bool)
            }
            if len(counts) == 1:
                only = counts.pop()
                if 0 < only <= 12:
                    return only
        for key in ("bathCount", "numBaths", "bathrooms", "baths"):
            value = record.get(key)
            # Several of these payloads describe a building rather than a
            # home, and write the range its plans cover. A range says what
            # this home has only once it has collapsed to a single number --
            # 30 of the 50 buildings on one ApartmentGuide page -- and the
            # other twenty stay unknown rather than being handed whichever
            # end of the range reads better.
            if isinstance(value, dict):
                # Written low/high by the Rent Group's pages and min/max by
                # Trulia's; the same range either way.
                low = value.get("low", value.get("min"))
                high = value.get("high", value.get("max"))
                value = low if low == high else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if 0 < float(value) <= 12:
                return float(value)
    return None


def _require_page(response: httpx.Response, platform: str) -> str:
    """Return a page of results, or refuse to read a wall as an empty result.

    Rate-limited, these sources answer with HTTP 202 and an empty body --
    observed on Rent.com and, read too often, on ApartmentGuide too.
    ``raise_for_status`` lets a 202 through and an empty body parses into zero
    listings, so a scan would record "this source found nothing today" at
    exactly the moment the source stopped talking to us. That is the fabricated
    zero the coverage rules already exist to refuse, so every way of saying
    "not a page of results" is raised here instead of being counted.
    """
    # Said in words before raise_for_status turns it into a class name. A
    # source row reading "HTTPStatusError: Client error '429 Too Many Requests'"
    # tells a reader nothing they can act on, and these two refuse often enough
    # that it would be the usual message rather than the rare one.
    if response.status_code in (403, 429):
        raise SourceError(
            f"{platform} turned away an unattended request (HTTP {response.status_code}). "
            "It is rate-limiting rather than broken; the next check tries again."
        )
    # A 5xx is the site being unwell, and it belongs in the same sentence rather
    # than in an HTTPStatusError. Two things went wrong when it did not: the
    # walk guards below do not catch an HTTPStatusError, so a 502 on page four
    # threw away the three pages already read, and the reader was shown
    # "HTTPStatusError: Server error '502 Bad Gateway' ... https://developer.mozilla.org/..."
    # as the reason their housing search had missed a source.
    if response.status_code >= 500:
        raise SourceError(
            f"{platform} answered HTTP {response.status_code}, which is the site having "
            "trouble rather than a page of results; the next check tries again."
        )
    # Any other 4xx in words too: httpx's own text for it is a URL and an MDN
    # link, and after a page of homes it ends the walk like any refusal.
    if response.status_code >= 400:
        raise SourceError(
            f"{platform} answered HTTP {response.status_code} rather than a page of results; "
            "the next check tries again."
        )
    if response.status_code != 200:
        raise SourceError(
            f"{platform} answered HTTP {response.status_code} rather than a page of results, "
            "which is how it turns away an unattended request."
        )
    text = response.text or ""
    if not text.strip():
        raise SourceError(f"{platform} returned an empty page rather than refusing outright.")
    return text


def _read_nothing(platform: str, document: str, what: str) -> SourceError:
    """Say why a page held nothing: a wall, or a shape this no longer reads.

    The block signals are only consulted here, and deliberately not on every
    page. They are whole-body substring tests written for counting a search
    result, and a three-megabyte listing page that merely mentions a captcha
    somewhere in its own JavaScript would fail two scans in a row and put a
    working source into a day of backoff. A page that parsed into real cards is
    not a wall whatever strings it happens to contain.
    """
    if looks_blocked(document):
        return SourceError(f"{platform} returned a bot check rather than results.")
    return SourceError(f"{platform} returned no {what}; its page format may have changed.")


def _bedroom_span(value: object) -> tuple[int, int] | None:
    """Read a bedroom count published as text, which is often a range.

    Redfin writes ``"0-3"`` for a building letting studios through
    three-bedrooms. Read as an integer that either raises or, worse, quietly
    becomes 0 and hides every three-bedroom in the building.
    """
    numbers = [int(found) for found in re.findall(r"\d+", str(value or ""))]
    if not numbers:
        return None
    return min(numbers), max(numbers)


def _wanted_bedroom_counts(preferences: Preferences) -> set[int]:
    """The bedroom counts this deal would rent, for whole homes."""
    enabled = set(preferences.deal_profile.enabled_paths)
    return {count for path, count in _WHOLE_UNIT_BEDROOMS.items() if path in enabled}


def _bedroom_floor(preferences: Preferences) -> int:
    wanted = _wanted_bedroom_counts(preferences)
    return min(wanted) if wanted else 0


def _is_san_francisco_locality(value: object) -> bool:
    """Does a structured address say San Francisco?

    Stated positively on purpose. A list of cities to reject can only reject
    the cities somebody thought of, and both of these pad a thin San Francisco
    search with whatever else is nearby.
    """
    return _normal_text(str(value or "")) == "san francisco"


def _outside_sf_note(value: object) -> str:
    """Name the city a padded result actually sits in, for the scan log."""
    return outside_sf_location_label(str(value or "")) or _clean_text(value, 40) or "an unstated city"


class RedfinSource:
    """Read the schema.org cards Redfin publishes for San Francisco rentals.

    Each rental card is one script tag holding two objects that share a URL: an
    ``Accommodation`` with the address, map pin, bedroom range and floor size,
    and a ``Product`` with the advertised rent. They are matched on that URL
    rather than by position, because the page also publishes unpaired
    ``Accommodation`` blocks for homes whose rent Redfin does not have, and
    pairing by position would print one building's rent against another.

    Redfin's own detail pages are deliberately never fetched. A building's page
    carries priced ``Product`` blocks for its *neighbours* and none at all for
    the home being viewed, so enrichment could only ever attach somebody else's
    rent. ``detail_budget`` is zero and the search page is the whole source.
    """

    platform = "Redfin"
    mode = "automatic"
    search_url = "https://www.redfin.com/city/17151/CA/San-Francisco/apartments-for-rent"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "Redfin published no San Francisco rentals in its structured data."
    # Each page is around three megabytes, and Redfin answers HTTP 202 when it
    # has had enough -- it was doing so while this was being measured. Six
    # pages is roughly 18MB and about 240 homes, which it has been seen to
    # serve; going deeper on a run somebody is waiting for risks trading a
    # working source for a rate-limited one, and a refused source returns
    # nothing at all.
    max_pages = 6
    # The nightly sweep goes further, at an hour where being turned away costs
    # nothing anybody sees. Kept well under Movoto's forty-five because each
    # page here is three megabytes rather than three hundred kilobytes.
    deep_max_pages = 15

    def _page_url(self, floor: int, page: int) -> str:
        # A bedroom floor narrows the page to homes worth reading. A rent
        # ceiling must never be added beside it: asked for both, Redfin pads the
        # results with whatever else it holds -- studios, rooms, a ten-bedroom,
        # homes in Mill Valley -- and the bedroom filter stops meaning anything.
        base = f"{self.search_url}/filter/min-beds={floor}" if floor else self.search_url
        return base if page == 1 else f"{base}/page-{page}"

    @staticmethod
    def _cards(document: str) -> list[tuple[dict, int | None]]:
        """Pair each Accommodation with the rent published against its own URL."""
        blocks = _json_ld_blocks(document)
        prices: dict[str, int] = {}
        for block in blocks:
            if "Product" not in _schema_types(block):
                continue
            url = str(block.get("url") or "").strip()
            price = _offer_price(block.get("offers"), "price")
            if url and price:
                prices[url] = price
        cards: list[tuple[dict, int | None]] = []
        for block in blocks:
            if "Accommodation" not in _schema_types(block):
                continue
            url = str(block.get("url") or "").strip()
            if url:
                cards.append((block, prices.get(url)))
        return cards

    def search_for_trigger(
        self, client: httpx.Client, preferences: Preferences, trigger: str
    ) -> list[ListingCandidate]:
        return self._search(client, preferences, _pages_for_trigger(self, trigger))

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search(client, preferences, self.max_pages)

    def _search(
        self, client: httpx.Client, preferences: Preferences, pages: int
    ) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False

        for page in range(1, pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.get(self._page_url(floor, page), headers=_BROWSER_HEADERS), self.platform
                )
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    break
                raise
            cards = self._cards(document)
            if not cards:
                break
            fresh = 0
            for block, price in cards:
                url = str(block.get("url") or "").strip()
                if url in seen:
                    continue
                seen.add(url)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(block, price, floor)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            # Past its last real page Redfin serves the first one again, so a
            # page that adds nothing new is the end of the results whatever its
            # own numbering claims.
            if not fresh:
                break

        if not read_a_card:
            raise _read_nothing(self.platform, document, "structured rental cards")
        return listings

    def _candidate(self, block: dict, price: int | None, floor: int) -> ListingCandidate | None:
        url = str(block.get("url") or "").strip()
        if not url.startswith("https://www.redfin.com/"):
            return None
        address = block.get("address") if isinstance(block.get("address"), dict) else {}
        # The feed is city-scoped, but Redfin widens a thin search without
        # saying so, and a Mill Valley home in a San Francisco search would
        # quietly defeat the point of searching San Francisco.
        if not _is_san_francisco_locality(address.get("addressLocality")):
            return None

        span = _bedroom_span(block.get("numberOfRooms"))
        # A building's range is what it lets, so its largest home decides
        # whether it is worth keeping: a "0-3" building really does have a
        # three-bedroom in it.
        if span is not None and span[1] < floor:
            return None

        street = _clean_text(address.get("streetAddress"), 160)
        name = _redfin_display_name(block.get("name")) or street
        if not name:
            return None

        geo = block.get("geo") if isinstance(block.get("geo"), dict) else {}
        metadata: dict[str, object] = {"building_listing": True}
        if street:
            metadata["address"] = street
        # No bathroom count here. Redfin's rental cards state none, and the
        # only "numBaths" anywhere on its search page belongs to the agents'
        # past-sale blurbs further down it -- somebody else's three-bedroom,
        # sold, with an escrow story attached. Reading that would put a
        # stranger's bathroom on a home for rent.
        detail = [f"{name} listed on Redfin."]
        if street:
            detail.append(f"Address: {street}.")

        published = price
        if span is not None:
            low, high = span
            # The home this card should stand for is the smallest one that
            # still meets the search. Anything smaller was excluded on purpose.
            representative = max(low, min(floor, high))
            metadata["bedrooms"] = representative
            metadata["bedrooms_low"], metadata["bedrooms_high"] = low, high
            if low == high:
                detail.append(f"Listed as {_bedroom_phrase(low)}.")
            else:
                detail.append(
                    f"This building lets {_bedroom_noun(low)} through {_bedroom_noun(high)} homes."
                )
            # Redfin publishes one rent per building and it belongs to the
            # smallest home in it. Printed against a larger one it would read as
            # a three-bedroom going for a studio's rent, so it is kept as the
            # building's starting rate and this home's rent stays unknown.
            if price is not None and representative != low:
                metadata["price_from"] = price
                detail.append(_starting_rate_note(price, low, representative))
                published = None
        if published is not None:
            detail.append(f"Advertised from ${published:,} a month.")
        elif price is None:
            detail.append("Redfin publishes no rent for this home yet.")

        floor_size = block.get("floorSize") if isinstance(block.get("floorSize"), dict) else {}
        size = _clean_text(floor_size.get("value"), 24)
        if size:
            metadata["floor_area"] = f"{size} sq ft"
            detail.append(f"Floor area {size} sq ft.")

        return ListingCandidate(
            platform=self.platform,
            source_id=_source_id(url),
            title=name,
            original_url=url,
            price=published,
            neighborhood=(
                sf_area_from_address(street)
                or sf_target_coordinate_neighborhood(geo.get("latitude"), geo.get("longitude"))
                or sf_area_from_zip(address.get("postalCode"))
                or visible_sf_area_hint(name)
            ),
            listing_type="Apartment building",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # Redfin's rental search lets whole homes, never a room inside one.
            housing_kind=WHOLE_UNIT,
        )


def _american_date(value: str) -> str | None:
    """An ISO date rewritten the one way ``scoring._available_on`` parses it."""
    try:
        parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _nested_mapping(payload: object, *keys: str) -> dict:
    """Walk a chain of keys, giving up rather than raising on any surprise.

    ``payload.get("props", {}).get("pageProps", {})`` reads safely only while
    every level really is a mapping. One level arriving as a list -- which is
    what a payload change looks like -- raises AttributeError from inside the
    parser, and the scan log gets a class name instead of "the format may have
    changed".
    """
    node: object = payload
    for key in keys:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _representative_bedroom(entries: object, wanted: set[int]) -> tuple[int | None, int | None]:
    """The bedroom count a deal asked for, and what that size costs.

    Rent.com and ApartmentGuide are one company on one codebase and publish the
    same ``bedCountData`` array -- a rent per bedroom count -- so unlike every
    other building source here the right home can be priced exactly rather than
    stood in for by the cheapest one in the building.

    A building the search returned that turns out to hold nothing of the wanted
    size keeps its own cheapest home instead, so the mismatch is scored rather
    than hidden. Where two sizes are equally cheap the smaller wins, and a size
    with no published rent sorts last: an unpriced studio must never displace a
    priced one-bedroom the deal actually asked for.
    """
    priced: list[tuple[int, int | None]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        beds = entry.get("beds")
        prices = entry.get("prices") if isinstance(entry.get("prices"), dict) else {}
        low = prices.get("low")
        if isinstance(beds, (int, float)) and not isinstance(beds, bool):
            rent = int(low) if isinstance(low, (int, float)) and not isinstance(low, bool) and low > 0 else None
            priced.append((int(beds), rent))
    if not priced:
        return None, None
    matching = [pair for pair in priced if pair[0] in wanted]
    return min(matching or priced, key=lambda pair: (pair[1] is None, pair[1] or 0, pair[0]))


def _other_bedroom_sizes(entries: object, representative: int | None) -> list[str]:
    """The building's *other* sizes, phrased as "a 1-bedroom from $2,740".

    The size this listing already stands for is left out. "Its studio homes
    start at $1,845. This building also lets a studio from $1,845" is one fact
    said twice, and reads as though the building had two different studios.
    """
    sizes: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        beds, prices = entry.get("beds"), entry.get("prices")
        low = prices.get("low") if isinstance(prices, dict) else None
        if (
            not isinstance(beds, (int, float))
            or isinstance(beds, bool)
            or not isinstance(low, (int, float))
            or isinstance(low, bool)
        ):
            continue
        if representative is not None and int(beds) == representative:
            continue
        sizes.append(f"{_bedroom_phrase(int(beds))} from ${int(low):,}")
    return sizes


def _numeric_span(low: object, high: object) -> tuple[int, int] | None:
    """A bedroom range published as two numbers, when both are really numbers.

    Either end missing makes the range unusable rather than half-known: a
    building whose smallest home is unknown cannot be excluded by a bedroom
    floor, and must not be quoted the starting rent of a size nobody stated.
    """
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return (int(min(low, high)), int(max(low, high)))


def _homes_free_note(count: int) -> str:
    """How many homes are free, in English.

    "1 homes are free right now" is what the obvious version prints, and a
    building with exactly one home left is the one a reader most wants to
    trust. Deliberately not the word "units": the building-size reader takes
    "N units" for the size of the whole building.
    """
    return "1 home is free right now." if count == 1 else f"{count} homes are free right now."


def _mentions_unit(address: str, unit: str) -> bool:
    """Does this address already carry this unit number?

    Compared on letters and digits alone, because the two fields spell the
    same unit differently: "#323" against "APT 323", "# 416" against "416".
    """
    def core(value: str) -> str:
        return re.sub(r"[^0-9a-z]", "", value.casefold())

    stripped = core(unit)
    # "APT 323" and "323" have to compare equal, so the label is dropped too.
    for label in ("apt", "unit", "ste", "suite", "no"):
        if stripped.startswith(label):
            stripped = stripped[len(label):]
            break
    return bool(stripped) and stripped in core(address)


def _bedroom_noun(count: int) -> str:
    return "studio" if count == 0 else f"{count}-bedroom"


def _bedroom_phrase(count: int) -> str:
    return f"a {_bedroom_noun(count)}"


def _starting_rate_note(price: int, low: int, representative: int) -> str:
    """Say whose rent this is, when it is not this home's.

    Three sources publish one rent for a building that lets several sizes, and
    it always belongs to the smallest home in it. Printed against a larger one
    it reads as a three-bedroom going for a studio's rent, so it is reported as
    the building's starting rate and this home's rent is left unstated.

    ``_bedroom_phrase`` already carries its own article, so the size is named
    here without one: "the a 2-bedroom rent is not published" is what reads
    otherwise, and it has been shipping.
    """
    return (
        f"Rents here start at ${price:,} a month for {_bedroom_phrase(low)}; "
        f"the {_bedroom_noun(representative)} rent is not published."
    )


def _redfin_display_name(value: object) -> str:
    """Redfin renders a missing building name as the literal word."""
    return re.sub(r"^undefined\s*-\s*", "", _clean_text(value, 180)).strip()


class RentComSource:
    """Read Rent.com's San Francisco buildings: cards to find them, its own
    hydration payload to learn what is actually in them.

    The search page publishes one ``ApartmentComplex`` card per building with
    little more than a name, an address and a link. Everything worth scoring —
    how many homes the building holds, the rent for each bedroom count, when a
    unit frees up, whether a room rather than a home is on offer — is in the
    ``__NEXT_DATA__`` payload on the building's own page, so buildings are
    completed there within the scanner's detail budget.

    Two behaviours shape the rest of this class. Asked for a bedroom count and
    a rent ceiling together, Rent.com quietly pads a thin result with homes in
    Oakland, Alameda, Berkeley and Tiburon and stops paginating: of ten cards
    on such a page, one was in San Francisco. It names the padding in an
    ``expandedSearchIds`` array, which is dropped here, and the rent ceiling is
    simply never asked for. And its ``?page=`` parameter is ignored while
    ``/page-N`` works right up to the last page, after which it silently serves
    the first one again.
    """

    platform = "Rent.com"
    mode = "automatic"
    search_url = "https://www.rent.com/california/san-francisco-apartments"
    manual_reason = None
    detail_budget = 12
    empty_result_message = "Rent.com published no San Francisco buildings in its structured data."
    # Ten pages is the whole San Francisco list -- 98 buildings, measured --
    # where three was reading the first 29 of them.
    max_pages = 10

    def __init__(self) -> None:
        # search() records what the deal asked for so enrich() can pick the home
        # the search was actually about. The scanner always searches a source
        # before enriching it, within one scan, on one instance, and scans hold
        # a file lock against each other, so this cannot be read across deals.
        self._wanted: set[int] = set()

    def _page_url(self, floor: int, page: int) -> str:
        base = f"{self.search_url}/{floor}-bedrooms" if floor else self.search_url
        return base if page == 1 else f"{base}/page-{page}"

    @staticmethod
    def _payload(document: str) -> dict:
        """Rent.com's hydration payload, or an empty mapping."""
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', document, re.S
        )
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return {}
        return _nested_mapping(parsed, "props", "pageProps", "pageData")

    @staticmethod
    def _listing_id(url: str) -> str | None:
        """The id Rent.com uses in expandedSearchIds, taken from the link."""
        match = re.search(r"-(l[a-z]\d+)/?$", str(url or "").strip())
        return match.group(1) if match else None

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        self._wanted = _wanted_bedroom_counts(preferences)
        floor = min(self._wanted) if self._wanted else 0
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False

        for page in range(1, self.max_pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.get(self._page_url(floor, page), headers=_BROWSER_HEADERS), self.platform
                )
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    break
                raise
            data = self._payload(document)
            # Past the last real page Rent.com serves page one again while its
            # own payload keeps saying "1". Believing the page would re-read the
            # same buildings until the budget ran out.
            if page > 1 and int(data.get("pageNumber") or 1) != page:
                break
            padded = {str(item) for item in (data.get("expandedSearchIds") or [])}
            cards = [
                block
                for block in _json_ld_blocks(document)
                if "ApartmentComplex" in _schema_types(block)
            ]
            if not cards:
                break
            fresh = 0
            for block in cards:
                url = str(block.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(block, padded)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            if not fresh:
                break

        if not read_a_card:
            raise _read_nothing(self.platform, document, "structured building cards")
        return listings

    def _candidate(self, block: dict, padded: set[str]) -> ListingCandidate | None:
        url = str(block.get("url") or "").strip()
        if not url.startswith("https://www.rent.com/"):
            return None
        # Rent.com names its own padding. Dropping it by id rather than by
        # guessing from the address catches a padded San Francisco address too.
        listing_id = self._listing_id(url)
        if listing_id and listing_id in padded:
            return None
        address = block.get("address") if isinstance(block.get("address"), dict) else {}
        if not _is_san_francisco_locality(address.get("addressLocality")):
            return None

        name = _clean_text(block.get("name"), 180)
        street = _clean_text(address.get("streetAddress"), 160)
        if not name:
            name = street
        if not name:
            return None

        metadata: dict[str, object] = {"building_listing": True, "detail_pending": True}
        if street:
            metadata["address"] = street
        baths = _stated_bathrooms(block)
        if baths is not None:
            metadata["bathrooms"] = baths

        return ListingCandidate(
            platform=self.platform,
            # The slug carries the building's name, so a rename would orphan the
            # stored row and add a duplicate rather than update it. Rent.com's
            # own id does not move.
            source_id=listing_id or _source_id(url),
            title=name,
            original_url=url,
            price=None,
            neighborhood=sf_area_from_address(street) or sf_area_from_zip(address.get("postalCode")),
            listing_type="Apartment building",
            # Deliberately the title, which is how this scanner asks for a
            # detail page it has not read. Everything worth scoring here is
            # behind that fetch, and a descriptive sentence in its place would
            # tell the scanner the building was already complete: one
            # rate-limited minute would strand it, priceless and sizeless, for
            # good.
            summary=name,
            metadata=metadata,
            housing_kind=WHOLE_UNIT,
        )

    def _representative(self, building: dict) -> tuple[int | None, int | None]:
        """The bedroom count this search was about, and what it costs."""
        return _representative_bedroom(building.get("bedCountData"), self._wanted)

    # Rent.com answers a building it no longer lets with a 404. Named here for
    # the same reason Craigslist names its own 404 and 410: raising instead
    # would make a home that is gone look exactly like a dropped connection,
    # and it would keep its place on the shortlist for good.
    GONE_STATUSES = frozenset({404, 410})

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        response = client.get(listing.original_url, headers=_BROWSER_HEADERS)
        if getattr(response, "status_code", 200) in self.GONE_STATUSES:
            return replace(
                listing,
                summary=_clean_text(f"{listing.title} is no longer listed on Rent.com.", 700),
                metadata={
                    **{k: v for k, v in listing.metadata.items() if k != "detail_pending"},
                    "verified_inactive": True,
                },
            )
        document = _require_page(response, self.platform)
        data = self._payload(document)
        building = data.get("listing") if isinstance(data.get("listing"), dict) else {}
        if not building:
            raise SourceError("Rent.com published no building payload on this page.")
        location = building.get("location") if isinstance(building.get("location"), dict) else {}
        # The scan client follows redirects, so a building that moved would put
        # another city's rents, size and move-in date on this listing. The card
        # was checked against the city; the page it actually served has to be
        # checked too, because a bare street line reads as a San Francisco
        # address whenever the city sits in a field of its own.
        if not _is_san_francisco_locality(location.get("city")):
            raise SourceError(
                f"Rent.com served a building in {_outside_sf_note(location.get('city'))} "
                "for a San Francisco listing."
            )
        served, asked = str(building.get("id") or ""), self._listing_id(listing.original_url)
        if asked and served and served != asked:
            raise SourceError(f"Rent.com served building {served} where {asked} was asked for.")

        metadata = {k: v for k, v in listing.metadata.items() if k != "detail_pending"}
        detail = [f"{listing.title} listed on Rent.com."]
        street = _clean_text(building.get("address"), 160) or str(metadata.get("address") or "")
        if street:
            metadata["address"] = street
            detail.append(f"Address: {street}.")

        # Rent.com states its own count. Kept on the field rather than in
        # metadata because the metadata reader stops at three digits, and a
        # 1,254-home building would be recorded as one of 125.
        units = building.get("totalUnits")
        building_units = int(units) if isinstance(units, (int, float)) and units > 0 else None

        beds, rent = self._representative(building)
        if beds is not None:
            metadata["bedrooms"] = beds
            if rent:
                detail.append(f"Its {_bedroom_noun(beds)} homes start at ${rent:,} a month.")
            else:
                detail.append(f"It lets {_bedroom_noun(beds)} homes, at a rent it does not publish.")
        sizes = _other_bedroom_sizes(building.get("bedCountData"), beds)
        if sizes:
            detail.append("This building also lets " + ", ".join(sizes[:6]) + ".")

        available = building.get("unitsAvailable")
        if isinstance(available, (int, float)) and available > 0:
            # Deliberately not phrased as "N units", which the building-size
            # reader would take for the size of the whole building.
            detail.append(_homes_free_note(int(available)))
            metadata["homes_available"] = int(available)
        if building.get("roomForRent") is True:
            detail.append("Rent.com lists this as a room rather than a whole home.")
        if building.get("offMarket") is True:
            # Said plainly so a home that is gone can leave the shortlist. Left
            # unsaid, a delisted building looks exactly like a dropped
            # connection and keeps its place forever.
            metadata["verified_inactive"] = True
            detail.append("Rent.com has taken this building off the market.")
        # Published as a list, so `is True` could never fire.
        if building.get("incomeRestrictions"):
            detail.append("This building is income restricted.")
            metadata["below_market_rate"] = True
        moves = sorted(
            {
                str(unit.get("dateAvailable"))[:10]
                for plan in building.get("floorPlans") or []
                if isinstance(plan, dict)
                for unit in plan.get("units") or []
                # A unit that is not available cannot be the earliest date
                # somebody could move in, however early its own stamp reads.
                if isinstance(unit, dict) and unit.get("dateAvailable") and unit.get("isAvailable")
            }
        )
        stated = _american_date(moves[0]) if moves else None
        if stated:
            # Written the way the scoring reads a move-in date. An ISO date
            # under a key of its own is a fact nothing consults, and the
            # availability criterion stays Unknown for the one source that
            # publishes the exact day.
            metadata["available_on"] = stated
            detail.append(f"Available {stated}.")
        # updatedAt is when Rent.com last touched its own record, not when the
        # home was posted. Stored as listing_timestamp it becomes published_at,
        # is rewritten to today on every scan, renders as "Posted today" and
        # pins every building to the top of the newest sort forever.
        updated = _clean_text(building.get("updatedAt"), 40)
        if updated:
            metadata["record_updated"] = updated

        neighborhood = (
            sf_area_from_address(street)
            or sf_target_coordinate_neighborhood(location.get("lat"), location.get("lng"))
            or sf_area_from_zip(location.get("zip"))
        )
        if neighborhood is None:
            # Rent.com names a neighbourhood of its own, which is only usable
            # where it happens to be one this deal can also rank.
            for area in location.get("neighborhoods") or []:
                if str(area) in SF_NEIGHBORHOODS:
                    neighborhood = str(area)
                    break

        return replace(
            listing,
            price=rent or listing.price,
            neighborhood=neighborhood or listing.neighborhood,
            listing_type=(
                f"{_bedroom_noun(beds).capitalize()} apartment"
                if beds is not None
                else listing.listing_type
            ),
            summary=_clean_text(" ".join(detail), 1200),
            building_units=building_units,
            housing_kind=ROOM if building.get("roomForRent") is True else WHOLE_UNIT,
            metadata=metadata,
        )


class ApartmentGuideSource:
    """Read the San Francisco buildings ApartmentGuide publishes.

    ApartmentGuide and Rent.com are one company on one codebase, and this is
    that same hydration payload read a second way. The difference is where the
    facts sit: Rent.com's search cards carry a name and an address and keep
    everything worth scoring behind a detail fetch, while ApartmentGuide's
    ``listingSearch.listings`` carries the whole building -- map pin, a rent per
    bedroom count, floor plans with their own counts and move-in dates. There is
    nothing behind a detail page worth spending a budget on, so ``detail_budget``
    is zero and one page read is one page of complete listings. That also means
    no listing here can be stranded by a rate-limited detail fetch, which is the
    failure ``detail_pending`` exists to recover from on Rent.com.

    ``filterMatchResults`` is a parallel projection of the same buildings, and
    the one place the upper end of each rent range and the floor areas are
    published. It is joined on ``listingId`` and never by position: both arrays
    came back in the same order on every page read here, which is exactly the
    kind of thing that silently stops being true and pairs one building's rent
    with another's address.
    """

    platform = "ApartmentGuide"
    mode = "automatic"
    search_url = "https://www.apartmentguide.com/apartments/California/San-Francisco/"
    manual_reason = None
    # The search page is the whole source; see the class docstring.
    detail_budget = 0
    empty_result_message = (
        "ApartmentGuide published no San Francisco buildings in its structured data."
    )
    # Fifty buildings a page against a city total of 476, so ten pages is all
    # of San Francisco and the rest is headroom: the repeat-guard below ends
    # the walk the moment a page comes back the same, so a ceiling above the
    # real depth costs one request rather than four wasted ones. Measured at
    # 491 buildings in 45 seconds, which is the slowest healthy source here
    # and still inside the scanner's per-source ceiling.
    max_pages = 14
    # Nothing is filtered in the request (see _page_url), the ceiling sits
    # above the real depth, and the repeat-guard ends the walk at the last
    # page of results, so a completed walk is the whole city list. Lowered
    # below for the walk that ends some other way.
    search_covers_inventory = True

    def _page_url(self, page: int) -> str:
        # Deliberately no bedroom or rent filter. ApartmentGuide accepts
        # `/N-bedrooms/` and `?maxPrice=` in the path and ignores both -- asked
        # for four-bedrooms under $2,000 it returned the same 476-building
        # first page -- so a filter here would only advertise a narrowing that
        # never happened. The bedroom count each deal wants is applied to the
        # payload instead, where it is real.
        return self.search_url if page == 1 else f"{self.search_url}?page={page}"

    @staticmethod
    def _payload(document: str) -> dict:
        """ApartmentGuide's building search, or an empty mapping."""
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', document, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return {}
        return _nested_mapping(
            parsed, "props", "pageProps", "pageData", "location", "listingSearch"
        )

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        wanted = _wanted_bedroom_counts(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False
        document = ""
        # Claimed afresh every walk, and given up the moment this one stops
        # somewhere other than the end of the results.
        self.search_covers_inventory = True

        for page in range(1, self.max_pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.get(self._page_url(page), headers=_BROWSER_HEADERS), self.platform
                )
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    # Turned away part way: the pages after this one hold
                    # buildings nobody looked at.
                    self.search_covers_inventory = False
                    break
                raise
            search = self._payload(document)
            buildings = [item for item in (search.get("listings") or []) if isinstance(item, dict)]
            if not buildings:
                break
            matched = {
                str(row.get("listingId")): row
                for row in (search.get("filterMatchResults") or [])
                if isinstance(row, dict) and row.get("listingId")
            }
            padded = {str(item) for item in (search.get("expandedSearchIds") or [])}
            fresh = 0
            for building in buildings:
                identifier = str(building.get("id") or "").strip()
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(building, matched.get(identifier), padded, wanted)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            # Past its last real page -- 476 buildings, so page ten --
            # ApartmentGuide serves page one again rather than an empty result,
            # with its own `total` still reading 476. Page eleven and page
            # ninety-nine both came back as page one. A page that adds no new
            # building is the end of the results whatever the payload claims.
            if not fresh:
                break
        else:
            # Fourteen pages read and the fourteenth still new: the walk ended
            # at its own ceiling rather than at the end of the city list.
            self.search_covers_inventory = False

        if not read_a_card:
            raise _read_nothing(self.platform, document, "structured building cards")
        return listings

    @staticmethod
    def _lettable_now(plan: dict) -> bool:
        """Can somebody rent this floor plan today?

        Two ways of saying yes, because ApartmentGuide uses both: a positive
        count, or a status that says available without one. Of the plans read
        here 123 were AVAILABLE_WITH_COUNT and 235 AVAILABLE_WITHOUT_COUNT, so
        testing the count alone would call two thirds of the lettable stock
        unavailable.
        """
        count = plan.get("availableCount")
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
            return True
        return str(plan.get("availabilityStatusCode") or "").startswith("AVAILABLE")

    def _candidate(
        self,
        building: dict,
        matched: dict | None,
        padded: set[str],
        wanted: set[int],
    ) -> ListingCandidate | None:
        identifier = str(building.get("id") or "").strip()
        # ApartmentGuide names its own padding in the same field Rent.com does.
        # Dropping it by id rather than by guessing from the address is what
        # catches a padded result that happens to carry a San Francisco address.
        if identifier in padded:
            return None
        location = building.get("location") if isinstance(building.get("location"), dict) else {}
        # The feed is city-scoped and every building read here was in San
        # Francisco, but this is the guard that has to hold when that changes:
        # one Oakland building scored against San Francisco rents would look
        # like the find of the week.
        if not _is_san_francisco_locality(location.get("city")):
            return None

        path = str(building.get("urlPathname") or "").strip()
        if not path:
            return None
        url = urljoin("https://www.apartmentguide.com", path)
        if not url.startswith("https://www.apartmentguide.com/"):
            # urljoin honours an absolute URL in `path`, so a payload carrying
            # somebody else's host would otherwise be stored and opened as-is.
            return None

        street = _clean_text(building.get("address"), 160)
        name = _clean_text(building.get("name"), 180) or street
        if not name:
            return None

        # filterMatchResults carries the high end of each rent range and the
        # floor areas; the listing's own copy carries only the low. Prefer the
        # richer one, fall back to the listing when the join finds nothing.
        rows = (matched or {}).get("bedCountData") or building.get("bedCountData")
        beds, rent = _representative_bedroom(rows, wanted)

        metadata: dict[str, object] = {"building_listing": True}
        if street:
            metadata["address"] = street
        detail = [f"{name} listed on ApartmentGuide."]
        if street:
            detail.append(f"Address: {street}.")

        if beds is not None:
            metadata["bedrooms"] = beds
            if rent:
                detail.append(f"Its {_bedroom_noun(beds)} homes start at ${rent:,} a month.")
            else:
                detail.append(f"It lets {_bedroom_noun(beds)} homes, at a rent it does not publish.")
        sizes = _other_bedroom_sizes(rows, beds)
        if sizes:
            detail.append("This building also lets " + ", ".join(sizes[:6]) + ".")

        baths = _stated_bathrooms(matched)
        if baths is not None:
            metadata["bathrooms"] = baths

        available = (matched or {}).get("totalAvailable")
        if isinstance(available, (int, float)) and not isinstance(available, bool) and available > 0:
            # Deliberately not phrased as "N units", which the building-size
            # reader would take for the size of the whole building. Nothing
            # ApartmentGuide publishes is the building's own size, so
            # `building_units` is left unset rather than guessed at from this.
            detail.append(_homes_free_note(int(available)))
            metadata["homes_available"] = int(available)

        # ApartmentGuide's `availableDate` is the day a floor plan *starts*
        # letting, and it is published only for plans that are not lettable
        # now: every one of the 103 dated plans read here was
        # UNAVAILABLE_WITH_FUTURE_MOVE_DATE with a count of zero, and every
        # plan free today carried no date at all. Reading the date as "this is
        # available from" and taking the earliest would therefore put a date
        # months out on a building with homes free this afternoon, and hand
        # scoring a move-in date later than the truth.
        plans = [plan for plan in building.get("floorPlans") or [] if isinstance(plan, dict)]
        if not any(self._lettable_now(plan) for plan in plans):
            # Nothing free today, so the first future date really is the
            # earliest somebody could move in.
            moves = sorted(
                {str(plan.get("availableDate"))[:10] for plan in plans if plan.get("availableDate")}
            )
            stated = _american_date(moves[0]) if moves else None
            if stated:
                # Written the one way `scoring._available_on` reads a date. An
                # ISO string under a key of its own is a fact nothing consults.
                metadata["available_on"] = stated
                detail.append(f"Nothing is free today; the first home here opens up {stated}.")

        area = _clean_text(building.get("squareFeetText"), 40)
        if area:
            metadata["floor_area"] = area
            detail.append(f"Floor area {area}.")

        offer = _clean_text(building.get("dealsText"), 240)
        if offer:
            # Said, never priced in. A concession changes what a year costs and
            # nothing about the rent, and folding it into `price` would put a
            # building below a budget it does not actually meet.
            # Terminated here because the concession is landlord copy and
            # arrives however it was typed: "*Restrictions May Apply" with no
            # full stop runs straight into the next sentence of the summary.
            detail.append(f"Offer: {offer}" if offer.endswith((".", "!", "?")) else f"Offer: {offer}.")
            metadata["concession"] = offer

        if building.get("offMarket") is True:
            # Said plainly so a building that is gone can leave the shortlist.
            # Left unsaid it looks exactly like a dropped connection and keeps
            # its place forever.
            metadata["verified_inactive"] = True
            detail.append("ApartmentGuide has taken this building off the market.")
        if building.get("incomeRestrictions"):
            detail.append("This building is income restricted.")
            metadata["below_market_rate"] = True

        manager = building.get("propertyManagementCompany")
        manager_name = _clean_text(manager.get("name"), 120) if isinstance(manager, dict) else ""
        if manager_name:
            metadata["managed_by"] = manager_name
            detail.append(f"Managed by {manager_name}.")

        updated = _clean_text(building.get("updatedAt"), 40)
        if updated:
            # When ApartmentGuide last touched its own record, not when the home
            # was posted. Stored as listing_timestamp it would become
            # published_at, be rewritten on every scan, render as "Posted today"
            # and pin every building to the top of the newest sort forever.
            metadata["record_updated"] = updated

        return ListingCandidate(
            platform=self.platform,
            # ApartmentGuide's own id. The path carries the building's name, so
            # a rename would orphan the stored row and add a duplicate rather
            # than update it.
            source_id=identifier,
            title=name,
            original_url=url,
            price=rent,
            neighborhood=(
                sf_area_from_address(street)
                or sf_target_coordinate_neighborhood(location.get("lat"), location.get("lng"))
                or sf_area_from_zip(location.get("zip") or building.get("zipCode"))
            ),
            listing_type=(
                f"{_bedroom_noun(beds).capitalize()} apartment"
                if beds is not None
                else "Apartment building"
            ),
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # ApartmentGuide lets whole homes, never a room inside one.
            housing_kind=WHOLE_UNIT,
        )


# 32767 is the largest signed 16-bit integer, and Zillow publishes it as a unit
# number where a home has none -- on Trulia too, which it owns. "1825 Mission St
# #32767" arrived from both, on a building card, which cannot have a unit number
# in the first place. Every other suffix seen was an ordinary flat number, so
# this strips the sentinel by value rather than guessing which numbers are real.
_NO_UNIT_SENTINEL = re.compile(r"\s*#\s*32767\b")


def _without_sentinel_unit(address: str) -> str:
    return _NO_UNIT_SENTINEL.sub("", address).strip()


class TruliaSource:
    """Read the San Francisco rental buildings Trulia publishes.

    One ``__NEXT_DATA__`` payload holds the whole result: ``searchData.homes``
    is one card per building, carrying the address as separate fields, the
    bedroom range, the floor area and a rent. Nothing worth scoring sits behind
    a detail page, so ``detail_budget`` is zero and no listing here can be
    stranded by a second request that never lands.

    Two things about this source shape the code. Its rent is a *range* --
    ``"$3,834 - $4,002/mo"`` -- and where a building lets more than one size the
    bottom of that range belongs to the smallest home in it, so the same rule
    Redfin needs applies here: quote it only against the smallest home the
    search would have accepted, and otherwise keep it as the building's
    starting rate with this home's own rent left unknown.

    And it refuses unattended requests hard. It answers 403 to the monitor's
    own User-Agent, 403 to a browser string that states no ``Accept-Language``,
    and -- read a few dozen times in a couple of minutes -- 403 to everything
    for a good while afterwards. A scan reads two pages a few times a day,
    which is nothing like that, but the refusal is common enough that it is
    reported as rate-limiting rather than breakage and simply retried on the
    next scheduled check. There is deliberately no retry loop here: retrying
    inside a scan is what turns an occasional refusal into a sustained one.
    """

    platform = "Trulia"
    mode = "automatic"
    search_url = "https://www.trulia.com/for_rent/San_Francisco,CA/"
    manual_reason = None
    # The search page is the whole source; see the class docstring.
    detail_budget = 0
    empty_result_message = "Trulia published no San Francisco rentals in its structured data."
    # Forty homes a page against 1,365 San Francisco rentals. Two pages is the
    # smallest footprint that still returns something, and on the runs somebody
    # is waiting for that is the right trade: every extra page is another
    # chance to be turned away for the next hour, and a refused source returns
    # nothing at all.
    max_pages = 2
    # The nightly sweep reads it to the bottom instead. Thirty-five pages is
    # the whole city; the repeat-guard stops the walk at the real end, and
    # being refused halfway through costs a scan nobody is watching rather
    # than the one on somebody's screen.
    deep_max_pages = 35

    def _page_url(self, page: int) -> str:
        # No bedroom filter. Trulia spells one `/2p_beds/`, and asked for it
        # alongside a city it answered 403 to every attempt while the unfiltered
        # page kept working. The bedroom count each deal wants is applied to the
        # payload instead, where it costs nothing.
        return self.search_url if page == 1 else f"{self.search_url}{page}_p/"

    @staticmethod
    def _homes(document: str) -> list[dict]:
        """The building cards on a Trulia search page, or nothing."""
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', document, re.S)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return []
        homes = _nested_mapping(parsed, "props", "searchData").get("homes")
        return [home for home in homes or [] if isinstance(home, dict)]

    def search_for_trigger(
        self, client: httpx.Client, preferences: Preferences, trigger: str
    ) -> list[ListingCandidate]:
        return self._search(client, preferences, _pages_for_trigger(self, trigger))

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search(client, preferences, self.max_pages)

    def _search(
        self, client: httpx.Client, preferences: Preferences, pages: int
    ) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False
        document = ""

        for page in range(1, pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.get(self._page_url(page), headers=_BROWSER_HEADERS), self.platform
                )
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    break
                raise
            homes = self._homes(document)
            if not homes:
                break
            fresh = 0
            for home in homes:
                identifier = self._home_id(home)
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(home, identifier, floor)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            # Past its last real page Trulia serves the first one again rather
            # than an empty result: page sixty came back as page one, card for
            # card. A page that adds no new building is the end of the results.
            if not fresh:
                break

        if not read_a_card:
            raise _read_nothing(self.platform, document, "structured rental cards")
        return listings

    @staticmethod
    def _home_id(home: dict) -> str:
        """Trulia's own id for the building.

        ``typedHomeId`` is the stable one. The URL carries the building's name
        and street, so keyed on that a rename orphans the stored row and adds a
        duplicate rather than updating it.
        """
        return _clean_text(home.get("typedHomeId") or home.get("providerListingId"), 60)

    def _candidate(self, home: dict, identifier: str, floor: int) -> ListingCandidate | None:
        location = home.get("location") if isinstance(home.get("location"), dict) else {}
        if not _is_san_francisco_locality(location.get("city")):
            return None

        # `homeUrl` is published as null on every card read here; `url` is the
        # real one and it is a path, not an address. Left as it arrives it
        # would be stored as "/building/..." and open nothing.
        path = str(home.get("url") or "").strip()
        if not path:
            return None
        url = urljoin("https://www.trulia.com", path)
        if not url.startswith("https://www.trulia.com/"):
            # urljoin honours an absolute URL in `path`, so a payload carrying
            # somebody else's host would otherwise be stored and opened as-is.
            return None

        street = _without_sentinel_unit(_clean_text(location.get("streetAddress"), 160))
        name = street or _clean_text(location.get("fullLocation"), 180)
        if not name:
            return None

        # Trulia states the bathrooms the same way it states the bedrooms: a
        # range over the building, which says what this home has only where
        # it has collapsed to one number. 99 of its homes on the owner's
        # board carried no count at all while this sat in the card.
        trulia_baths = _stated_bathrooms(home)
        bedrooms = home.get("bedrooms") if isinstance(home.get("bedrooms"), dict) else {}
        # Read as numbers rather than formatted into a string for _bedroom_span:
        # a card with a max and no min renders as "None 2", from which the span
        # reader takes the single number it can find and reports a
        # two-bedroom floor for a building whose smallest home is unknown.
        span = _numeric_span(bedrooms.get("min"), bedrooms.get("max"))
        if span is not None and span[1] < floor:
            # A building's range is what it lets, so its largest home decides
            # whether it is worth keeping at all.
            return None

        price_text = _clean_text(
            (home.get("price") or {}).get("formattedPrice") if isinstance(home.get("price"), dict) else None,
            60,
        )
        # A range reads as its lower end, which is what `_parse_price` returns.
        price = _parse_price(price_text, require_currency=True)

        metadata: dict[str, object] = {"building_listing": True}
        if street:
            metadata["address"] = street
        if trulia_baths is not None:
            metadata["bathrooms"] = trulia_baths
        detail = [f"{name} listed on Trulia."]
        if street:
            detail.append(f"Address: {street}.")

        published = price
        if span is not None:
            low, high = span
            # The home this card stands for is the smallest one that still
            # meets the search; anything smaller was excluded on purpose.
            representative = max(low, min(floor, high))
            metadata["bedrooms"] = representative
            metadata["bedrooms_low"], metadata["bedrooms_high"] = low, high
            if low == high:
                detail.append(f"Listed as {_bedroom_phrase(low)}.")
            else:
                detail.append(
                    f"This building lets {_bedroom_noun(low)} through {_bedroom_noun(high)} homes."
                )
            # The bottom of Trulia's range belongs to the smallest home in the
            # building. Printed against a larger one it reads as a two-bedroom
            # going for a studio's rent, so it stays the building's starting
            # rate and this home's rent stays unknown.
            if price is not None and representative != low:
                metadata["price_from"] = price
                detail.append(_starting_rate_note(price, low, representative))
                published = None
        if published is not None:
            detail.append(
                f"Advertised at {price_text}." if "-" in price_text
                else f"Advertised at ${published:,} a month."
            )
        elif price is None:
            detail.append("Trulia publishes no rent for this home yet.")

        floor_space = home.get("floorSpace") if isinstance(home.get("floorSpace"), dict) else {}
        area = _clean_text(floor_space.get("formattedDimension"), 40)
        if area:
            metadata["floor_area"] = area
            detail.append(f"Floor area {area}.")

        # Trulia's own labels: "SPECIAL OFFER" is a concession worth seeing on
        # a card, and the rest say what the building allows.
        labels = [
            _clean_text(tag.get("formattedName"), 40)
            for tag in home.get("tags") or []
            if isinstance(tag, dict) and tag.get("formattedName")
        ]
        if labels:
            detail.append("Trulia tags it " + ", ".join(label.lower() for label in labels[:4]) + ".")

        # A room inside somebody's home is not a whole flat, and scoring the
        # two the same way is how a lodger's room reaches a whole-home deal.
        kind = ROOM if "room" in str(home.get("__typename") or "").casefold() else WHOLE_UNIT

        return ListingCandidate(
            platform=self.platform,
            source_id=identifier,
            title=name,
            original_url=url,
            price=published,
            neighborhood=(
                sf_area_from_address(street)
                or sf_area_from_zip(location.get("zipCode"))
            ),
            listing_type="Room in a home" if kind == ROOM else "Apartment building",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            housing_kind=kind,
        )


class MovotoSource:
    """Read the individual San Francisco homes Movoto has out to let.

    Almost every other automatic source here publishes *buildings*: one card
    for an address, with a rent that belongs to whichever home in it is
    cheapest. Movoto publishes the homes themselves -- 1,964 of them across
    forty pages, each with its own rent, its own unit number and its own
    bedroom count -- which is a different shape of inventory rather than more
    of the same, and it is why a third of the addresses read here were ones no
    other source had.

    Two payloads sit on the page and only one is worth reading. The
    schema.org blocks carry an address and nothing else: no bedrooms, no floor
    area, and the rent in a separate ``Product`` keyed on a URL that the
    residence block does not repeat. The ``__INITIAL_STATE__`` script behind
    them carries the whole record -- rent, bedrooms, bathrooms, floor area,
    the unit number, and Movoto's own name for the neighbourhood -- so that is
    what is read, and the schema.org blocks are ignored entirely.

    The page is a rentals search, but nothing about a listing's shape says so:
    a home for sale and a home to let are the same record with a different
    status, and its ``listPrice`` is a sale price on one and a monthly rent on
    the other. So every record is checked against ``houseRealStatus`` before
    it is believed. Read without that, one search returning sale listings
    would put million-dollar "rents" into the pool.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Movoto"
    mode = "automatic"
    search_url = "https://www.movoto.com/san-francisco-ca/rentals/"
    manual_reason = None
    # Every field worth scoring is on the search page.
    detail_budget = 0
    empty_result_message = "Movoto published no San Francisco rentals in its page data."
    # Fifty homes a page and forty pages of them, which is the whole San
    # Francisco inventory: 1,967 homes read in about eleven seconds, because
    # each page is a single request and a JSON parse. The ceiling sits above
    # the real depth on purpose -- the repeat-guard below ends the walk as
    # soon as a page comes back the same, so headroom for a bigger inventory
    # costs one request rather than five wasted ones.
    max_pages = 45
    # Nothing is filtered in the request, the ceiling sits above the real
    # depth, and the repeat-guard ends the walk at the last page of results,
    # so a completed walk really is the whole San Francisco list. Lowered
    # below for the walk that ends some other way.
    search_covers_inventory = True

    # The status that means "this is a home to let". Checked rather than
    # assumed from the URL, because the record shape is identical for a sale.
    FOR_RENT = "FOR_RENT"

    def _page_url(self, page: int) -> str:
        return self.search_url if page == 1 else f"{self.search_url}p-{page}/"

    @staticmethod
    def _listings(document: str) -> list[dict]:
        """The homes on a Movoto search page, or nothing."""
        match = re.search(
            r'<script id="__INITIAL_STATE__"[^>]*>(.*?)</script>', document, re.S
        )
        if not match:
            return []
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return []
        rows = _nested_mapping(parsed, "pageData").get("listings")
        return [row for row in rows or [] if isinstance(row, dict)]

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        wanted = _wanted_bedroom_counts(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False
        document = ""
        # Claimed afresh every walk, and given up the moment this one stops
        # somewhere other than the end of the results.
        self.search_covers_inventory = True

        for page in range(1, self.max_pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.get(self._page_url(page), headers=_BROWSER_HEADERS), self.platform
                )
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    # Turned away part way: the pages after this one hold homes
                    # nobody looked at, and they are not missing from anything.
                    self.search_covers_inventory = False
                    break
                raise
            homes = self._listings(document)
            if not homes:
                break
            fresh = 0
            for home in homes:
                identifier = _clean_text(home.get("mlsNumber") or home.get("id"), 60)
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(home, identifier, wanted)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            # Forty pages of results, then page one again: page forty holds the
            # last fourteen homes, and pages forty-five and fifty both came
            # back as page one, home for home. A page that adds nothing new is
            # the end of the results whatever its own total claims.
            if not fresh:
                break
        else:
            # Forty-five pages read and the forty-fifth still new: the walk
            # ended at its own ceiling rather than at the end of Movoto.
            self.search_covers_inventory = False

        if not read_a_card:
            raise _read_nothing(self.platform, document, "homes in its page data")
        return listings

    def _candidate(
        self, home: dict, identifier: str, wanted: set[int]
    ) -> ListingCandidate | None:
        # The one check that stops a sale listing being read as a rent. Every
        # home on this page said FOR_RENT; a page that ever returns something
        # else is returning a different kind of thing, not a cheaper home.
        if _clean_text(home.get("houseRealStatus"), 40).upper() != self.FOR_RENT:
            return None
        if home.get("isRentals") is False or home.get("isSold") is True:
            return None

        geo = home.get("geo") if isinstance(home.get("geo"), dict) else {}
        if not _is_san_francisco_locality(geo.get("city")):
            return None

        path = str(home.get("path") or "").strip()
        if not path:
            return None
        url = urljoin("https://www.movoto.com/", path)
        if not url.startswith("https://www.movoto.com/"):
            # urljoin honours an absolute URL in `path`, so a payload carrying
            # somebody else's host would otherwise be stored and opened as-is.
            return None

        street = _clean_text(geo.get("address"), 160)
        # The unit number is what separates two homes at one address. Kept in
        # the title rather than the address so corroboration still matches the
        # building, which is the thing another source can confirm.
        #
        # Movoto writes it in both fields on most homes and only one on some:
        # "1140 Harrison St #323" arrives with a subPremise of "APT 323".
        # Appended unconditionally that reads "1140 Harrison St #323 APT 323",
        # so it is added only where the address has not already got it.
        unit = _clean_text(geo.get("subPremise"), 24)
        name = street or _clean_text(geo.get("formatAddress"), 180)
        if unit and not _mentions_unit(street, unit):
            name = f"{name} {unit}".strip()
        if not name:
            return None

        rent = home.get("listPrice")
        price = int(rent) if isinstance(rent, (int, float)) and not isinstance(rent, bool) and rent > 0 else None

        metadata: dict[str, object] = {}
        if street:
            metadata["address"] = street
        if unit:
            metadata["unit"] = unit
        detail = [f"{name} listed on Movoto."]
        if street:
            detail.append(f"Address: {street}.")

        beds = home.get("bed")
        bedrooms = int(beds) if isinstance(beds, (int, float)) and not isinstance(beds, bool) else None
        if bedrooms is not None:
            metadata["bedrooms"] = bedrooms
            detail.append(f"Listed as {_bedroom_phrase(bedrooms)}.")
        else:
            # Roughly one home in seven publishes no bedroom count. Said out
            # loud rather than defaulted to a studio, which is what an
            # unstated count silently becomes wherever zero is the fallback.
            detail.append("Movoto does not state how many bedrooms this home has.")

        baths = home.get("bath")
        if isinstance(baths, (int, float)) and not isinstance(baths, bool) and baths > 0:
            plural = "" if baths == 1 else "s"
            detail.append(f"{int(baths) if float(baths).is_integer() else baths} bathroom{plural}.")

        area = home.get("sqftTotal")
        if isinstance(area, (int, float)) and not isinstance(area, bool) and area > 0:
            metadata["floor_area"] = f"{int(area):,} sq ft"
            detail.append(f"Floor area {int(area):,} sq ft.")

        if price is not None:
            detail.append(f"Asking ${price:,} a month.")

        kind = _clean_text(home.get("propertyType"), 40).replace("_", " ").title()

        # Movoto names the neighbourhood itself, which beats inferring one from
        # the street -- but only where it names one this deal can also rank.
        # Nineteen of the thirty-five it used here are names the app knows.
        stated = _clean_text(geo.get("neighborhoodName"), 60)
        neighborhood = (
            stated if stated in SF_NEIGHBORHOODS else None
        ) or sf_area_from_address(street) or sf_target_coordinate_neighborhood(
            geo.get("lat"), geo.get("lng")
        ) or sf_area_from_zip(geo.get("zipcode"))
        # Worth saying only where it disagrees with the label this listing got.
        # "Movoto files it under Parkmerced" against a listing already labelled
        # Park Merced is the same fact spelled differently.
        if stated and _normal_text(stated).replace(" ", "") != _normal_text(
            str(neighborhood or "")
        ).replace(" ", ""):
            detail.append(f"Movoto files it under {stated}.")

        return ListingCandidate(
            platform=self.platform,
            # Movoto's own listing id. The path carries the street and the unit,
            # so keyed on that a re-listing at a corrected address would orphan
            # the stored row rather than update it.
            source_id=identifier,
            title=name,
            original_url=url,
            price=price,
            neighborhood=neighborhood,
            listing_type=kind or "Home",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # One home let whole, not a room inside somebody else's.
            housing_kind=WHOLE_UNIT,
        )


# Zillow writes its status into the page's own data, sometimes inside a JSON
# string and so escaped, sometimes not. A page for a home still to let carries
# it sixteen or seventeen times over; one that is off the market carries only
# "OTHER", for the home and for the ones it suggests instead.
_ZILLOW_FOR_RENT = re.compile(r'\\?"homeStatus\\?"\s*:\s*\\?"FOR_RENT\\?"')

# What a reader sees on the same page: the chip over the photographs, and the
# button Zillow offers the owner in place of a rent.
_ZILLOW_OFF_MARKET = re.compile(r"Off market|Claim home")


class ZillowSource:
    """Read the San Francisco rentals Zillow publishes on its own search page.

    Zillow was a setup source here for as long as this app has existed: connect
    an inbox, save a search on Zillow, wait for it to email you. It had brought
    in nothing. The page itself turns out to answer an ordinary request --
    including, when this was written, one that identified itself honestly --
    and to carry its results in the page rather than behind an API call. It is
    read with the browser string in ``_BROWSER_HEADERS`` all the same, and
    docs/sources.md says so rather than describing the honest request it once
    answered.

    They sit under ``searchPageState.cat1.searchResults.listResults``, 41 to a
    page against a stated 2,568, in two shapes that have to be read differently:

    * a building, with ``units`` holding one entry per bedroom count it lets
      and a rent written as text ("$3,886+"), and
    * a single home, with ``unformattedPrice``, ``beds``, ``baths`` and
      ``area`` as numbers.

    Read as one shape, four homes in every page of 41 lose their bedroom count
    and 37 lose their rent.

    Zillow owns Trulia, and the two share a quirk this relies on knowing:
    32767 -- the largest signed 16-bit integer -- appears as a unit number
    where a home has none.
    """

    platform = "Zillow"
    # Its cards say FOR_RENT for homes its own pages call off the market, so
    # a card coming back proves nothing and the page is read instead. See
    # ``card_proves_listed`` and ``enrich`` below.
    search_card_proves_listed = False
    # Five pages a scan, where every other source may read 250.
    #
    # Zillow answers about thirty detail pages from one address before its bot
    # protection starts returning a captcha to everything -- measured: after
    # thirty-five reads in a few minutes, the *search page itself* began
    # answering 403. Losing the search costs every home this source finds, to
    # learn about the handful being read, so the reading is kept far below
    # whatever that threshold really is.
    #
    # Few, and the right ones. A home whose page has never been read sorts to
    # the front of the queue (``Repository.shortlisted_absent_from_search``),
    # and a home that arrived in this scan is exactly that -- the recheck runs
    # after the scan has stored what it found -- so new homes are checked as
    # they land, before they are ever shown as a shortlist a reader can act
    # on. About three of those arrive per scan; the rest of the five works
    # through the homes already there.
    recheck_budget = 5
    recheck_floor = 5
    mode = "automatic"
    search_url = "https://www.zillow.com/san-francisco-ca/rentals/"
    manual_reason = None
    # Everything worth scoring is on the search page.
    detail_budget = 0
    empty_result_message = "Zillow published no San Francisco rentals on its search page."
    # Forty-one homes a page, and Zillow serves twenty-four of them: page 25
    # is refused outright however patiently it is asked. That is a cap on the
    # results rather than on the request rate -- about a thousand homes -- so
    # the 2,568 the page claims is not reachable by paging, and six pages was
    # leaving three quarters of what is reachable unread.
    max_pages = 24
    # The nightly sweep asks for a few more than that. Reaching the wall is
    # handled below rather than being an error, so probing costs one refused
    # request and finds the extra homes on any day Zillow lifts the cap.
    deep_max_pages = 30

    # A second between pages, because Zillow blocks by address and it lasts.
    # Roughly three hundred requests over half an hour -- ten a minute, spread
    # over an afternoon of measuring -- earned a 403 that outlived six hours,
    # on both a browser string and an honest one. Unpaced, eight bands is
    # fifty-two requests inside a minute: a burst rate five times the average
    # that drew the block, saved so far only by being short. This halves it and
    # costs nothing that matters -- an interactive read stays near thirty-five
    # seconds against a seventy-five second ceiling, and the sweep near two
    # minutes against five.
    PAGE_PAUSE_SECONDS = 1.0

    # And a floor between whole reads. The pause above spaces the pages of one
    # read; this spaces the reads. Every scan re-read every source in full, so
    # somebody pressing Check for new homes while waiting spent twenty-four
    # requests a press -- the surest way for one of these to earn a block that
    # outlasts the afternoon. Fifteen minutes caps that at four reads an hour,
    # which is under two requests a minute even if somebody never stops
    # pressing. San Francisco rentals do not turn over inside fifteen minutes,
    # so nothing is missed; the nightly sweep is exempt and still reads deeply.
    min_seconds_between_reads = 900

    # What "there is no such page" looks like once a search has produced
    # homes. The plain search answers 400 past its last page; 404 and 410 say
    # the same thing in other words. None of them is Zillow declining to serve.
    END_OF_RESULTS = frozenset({400, 404, 410})

    FOR_RENT = "FOR_RENT"

    # Rent bands, measured against live Zillow rather than guessed. The cap is
    # on results, not on requests, so the only way past it is to ask several
    # narrower questions -- and that only works while each one lands under it:
    #
    #     under 2000    84      4000-5500   671
    #     2000-2600    122      5500-7000   482
    #     2600-3200    149      7000-9000   260
    #     3200-4000    367      9000 up     162
    #
    # 2,297 homes against the 2,565 Zillow states, where one unsliced search
    # reaches 984. San Francisco rent is skewed high, which is why these are
    # not evenly spaced: an even split puts well over a thousand homes in one
    # band, and the overflow is lost exactly as silently as before.
    PRICE_BANDS: tuple[tuple[int | None, int | None], ...] = (
        (None, 2000),
        (2000, 2600),
        (2600, 3200),
        (3200, 4000),
        (4000, 5500),
        (5500, 7000),
        (7000, 9000),
        (9000, None),
    )

    # Where the rent bands are asked for: Zillow's older search address, whose
    # paginated form is one its robots.txt names as allowed. See _band_url.
    BAND_SEARCH_URL = "https://www.zillow.com/homes/for_rent/San-Francisco,-CA_rb/"

    def _band_url(self, band: tuple[int | None, int | None], page: int) -> str:
        """One rent band's search, in a form Zillow's robots.txt allows.

        The bands used to be asked for as Zillow's own query state,
        ``?searchQueryState={...}``, and ``Disallow: /*?searchQueryState=*``
        sits in the rules Zillow publishes for every crawler. Nothing else
        filtered: on the address the plain search uses, the path forms that
        look like filters -- ``2500-3500_price/`` and ``2500-3500_mp/`` -- are
        accepted and then ignored, answering 200 with the same unfiltered page,
        12 of 94 prices inside the band asked for.

        Under ``/homes/for_rent/`` the same rent path is read, and a path there
        ending ``_p/`` is on the allowed list (``Allow: /homes/for_rent/*_p/$``,
        which outranks ``Disallow: /homes/`` by being the longer match). Checked
        on 18 September with one request per shape this writes -- 2000-2600,
        9000 and up, and 0-2000 -- each answered 200 with no redirect, the page
        Zillow's parser here already reads, rentals only, Zillow's own echo of
        the search reading the band that was asked for (``"monthlyPayment":
        {"min": 2000, "max": 2600}``), every rent inside it, the page number
        asked for, and the same depth -- 3, 4 and 2 pages -- that the query-state
        form had served the night before. Page one is asked for as ``1_p/``
        because the bare path is not on the allowed list.

        The bedroom pages robots.txt also allows (``/homes/apartments-2-
        bedrooms/...``) looked like another way to slice and are not: Zillow
        reads them as featured apartment communities only, four pages deep.
        """
        low, high = band
        rent = f"{low or 0}-{'' if high is None else high}_mp"
        return f"{self.BAND_SEARCH_URL}{rent}/{page}_p/"

    def _page_url(self, page: int) -> str:
        return self.search_url if page == 1 else f"{self.search_url}{page}_p/"

    @staticmethod
    def _search_state(document: str) -> dict:
        """What a Zillow search page says about itself, or nothing."""
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', document, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return {}
        return _nested_mapping(parsed, "props", "pageProps", "searchPageState")

    @staticmethod
    def _rows(state: dict) -> list[dict]:
        found = _nested_mapping(state, "cat1", "searchResults").get("listResults")
        return [row for row in found or [] if isinstance(row, dict)]

    @classmethod
    def _results(cls, document: str) -> list[dict]:
        """The rentals on a Zillow search page, or nothing."""
        return cls._rows(cls._search_state(document))

    @staticmethod
    def _band_ignored(state: dict, band: tuple[int | None, int | None]) -> bool:
        """Whether Zillow says it ran some search other than this rent band.

        Every page echoes the search it answered. That echo is the only thing
        that can tell a band from the unfiltered search, because both look like
        a page of San Francisco rentals -- which is how the path filters on the
        plain search address went unnoticed until the prices were counted. A
        band Zillow stops reading would otherwise be walked to the bottom eight
        times a night, eight copies of the plain search. A page that echoes
        nothing is read as before.
        """
        filters = _nested_mapping(state, "queryState", "filterState")
        if not filters:
            return False
        served = filters.get("monthlyPayment") or filters.get("mp")
        if not isinstance(served, dict):
            return True
        low, high = band
        return (served.get("min") or 0, served.get("max")) != (low or 0, high)

    def search_for_trigger(
        self, client: httpx.Client, preferences: Preferences, trigger: str
    ) -> list[ListingCandidate]:
        # Slicing costs several times the requests, so it belongs to the run
        # nobody is waiting on. A check somebody pressed reads the one plain
        # search and stays inside ten seconds.
        bands = self.PRICE_BANDS if trigger == DEEP_SWEEP_TRIGGER else (None,)
        return self._search(client, preferences, _pages_for_trigger(self, trigger), bands)

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search(client, preferences, self.max_pages)

    def _search(
        self,
        client: httpx.Client,
        preferences: Preferences,
        pages: int,
        bands: tuple[tuple[int | None, int | None] | None, ...] = (None,),
    ) -> list[ListingCandidate]:
        wanted = _wanted_bedroom_counts(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False
        document = ""
        # Counted across every band: the pause is about the rate Zillow sees
        # from this address, which does not reset because a new band started.
        requested = 0

        # ``seen`` spans every band on purpose: a building whose rents straddle
        # a boundary is returned on both sides of it, and the pool wants it once.
        for band in bands:
            # Whether a page is the band's last is a question about that band's
            # own pages. Asking it of the global set ended a band the moment it
            # returned anything an earlier band had already produced, which cost
            # roughly a thousand homes before it was measured.
            band_seen: set[str] = set()
            for page in range(1, pages + 1):
                url = self._page_url(page) if band is None else self._band_url(band, page)
                if requested:
                    time.sleep(self.PAGE_PAUSE_SECONDS)
                requested += 1
                try:
                    # A band's page is never followed anywhere. Past a band's
                    # last page Zillow redirected the query-state form to the
                    # start of the unfiltered search, and following it counted
                    # that page as the band's next one and then asked for
                    # another: four requests at the end of every band, thirty-two
                    # of the sixty-nine the sweep sent on 18 September, for homes
                    # the plain search already reads. The path form answers the
                    # same way -- run live on 18 September, every band ended in
                    # one 301 -- so a redirect is the end of the band, as is
                    # anything below that says "no such page".
                    response = client.get(
                        url, headers=_BROWSER_HEADERS, follow_redirects=band is None
                    )
                    if band is not None and 300 <= response.status_code < 400:
                        break
                    # Past its last page Zillow answers 400 rather than serving
                    # an empty one. Once cards have been read that is the end of
                    # this search, not the loss of it. Only statuses that mean
                    # "no such page" end it quietly: a 403 is Zillow turning us
                    # away rather than running out of homes, and reading it as
                    # an ending would collect a little, report success, and come
                    # back tomorrow to be turned away again.
                    if read_a_card and response.status_code in self.END_OF_RESULTS:
                        break
                    document = _require_page(response, self.platform)
                except (SourceError, httpx.HTTPError) as failure:
                    # PARTIAL_WALK, the way Zillow needs it. With nothing read
                    # yet the failure is the whole story, and a source that is
                    # really blocked is reported as blocked. With homes in hand
                    # the walk stops -- the band after a refusal is only another
                    # refusal, and asking again is how a block gets longer --
                    # keeps every one of them, and still reports the failure, so
                    # two in a row pause Zillow as they always did.
                    if not read_a_card:
                        raise
                    raise PartialReadError(failure, listings) from failure
                state = self._search_state(document)
                results = self._rows(state)
                if not results:
                    break
                fresh = 0
                for home in results:
                    identifier = _clean_text(home.get("id") or home.get("zpid"), 60)
                    if not identifier or identifier in band_seen:
                        continue
                    band_seen.add(identifier)
                    fresh += 1
                    read_a_card = True
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    candidate = self._candidate(home, identifier, wanted)
                    if candidate is not None:
                        listings.append(candidate)
                        if len(listings) >= maximum:
                            return listings
                # Its homes are real San Francisco rentals and are kept, but a
                # band Zillow did not apply is not walked any further.
                if band is not None and self._band_ignored(state, band):
                    LOGGER.warning(
                        "Zillow did not apply the %s rent band on page %s, so that band is read no further",
                        band,
                        page,
                    )
                    break
                # A page that repeats this band's own results is its last,
                # whatever its numbering claims.
                if not fresh:
                    break

        if not read_a_card:
            raise _read_nothing(self.platform, document, "rentals on its search page")
        return listings

    @staticmethod
    def _building_units(home: dict) -> list[tuple[int, int | None, bool]]:
        """A building's sizes, as (bedrooms, rent, is a room) -- text parsed.

        Zillow writes these as strings: ``{"price": "$3,886+", "beds": "1"}``.
        A studio arrives as a bedroom count of "0" or as "Studio", so both are
        read rather than one of them silently becoming nothing.
        """
        sizes: list[tuple[int, int | None, bool]] = []
        for unit in home.get("units") or []:
            if not isinstance(unit, dict):
                continue
            raw = _clean_text(unit.get("beds"), 24)
            if not raw:
                continue
            bedrooms = 0 if _normal_text(raw).startswith("studio") else None
            if bedrooms is None:
                digits = re.search(r"\d+", raw)
                if not digits:
                    continue
                bedrooms = int(digits.group(0))
            sizes.append(
                (bedrooms, _parse_price(_clean_text(unit.get("price"), 40), require_currency=True),
                 unit.get("roomForRent") is True)
            )
        return sizes

    def _candidate(
        self, home: dict, identifier: str, wanted: set[int]
    ) -> ListingCandidate | None:
        # The page is a rentals search, but the record shape is the same one
        # Zillow uses for a sale. Checked rather than assumed from the URL.
        if _clean_text(home.get("statusType"), 40).upper() != self.FOR_RENT:
            return None
        if not _is_san_francisco_locality(home.get("addressCity")):
            return None

        path = str(home.get("detailUrl") or "").strip()
        if not path:
            return None
        # Zillow gives some of these as a path and some as a full address.
        url = urljoin("https://www.zillow.com/", path)
        if not url.startswith("https://www.zillow.com/"):
            return None
        # One building can appear twice on a page: once as itself, once as a
        # specific unit inside it, both pointing at the same detail page. Vara
        # arrived as a studio building at $3,852 and as 1863 Mission St #306 at
        # $3,300. `canonical_url` is unique, so stored as-is the second silently
        # overwrites the first and one of the two homes is simply lost. The id
        # is what tells them apart, so it goes in the link -- kept by
        # `canonicalize_url`, which only drops campaign tags.
        url = f"{url}{'&' if '?' in url else '?'}zid={quote(identifier)}"

        street = _without_sentinel_unit(_clean_text(home.get("addressStreet"), 160))
        name = _clean_text(home.get("buildingName"), 180) or street
        if not name:
            return None

        metadata: dict[str, object] = {}
        if street:
            metadata["address"] = street
        detail = [f"{name} listed on Zillow."]
        if street and street != name:
            detail.append(f"Address: {street}.")

        sizes = self._building_units(home)
        is_room = bool(sizes) and all(size[2] for size in sizes)
        if sizes:
            metadata["building_listing"] = True
            beds, rent = _representative_bedroom(
                [{"beds": bedrooms, "prices": {"low": price}} for bedrooms, price, _ in sizes], wanted
            )
            if beds is not None:
                metadata["bedrooms"] = beds
                if rent:
                    detail.append(f"Its {_bedroom_noun(beds)} homes start at ${rent:,} a month.")
                else:
                    detail.append(f"It lets {_bedroom_noun(beds)} homes, at a rent it does not publish.")
            others = _other_bedroom_sizes(
                [{"beds": bedrooms, "prices": {"low": price}} for bedrooms, price, _ in sizes], beds
            )
            if others:
                detail.append("This building also lets " + ", ".join(others[:6]) + ".")
            price = rent
            free = home.get("availabilityCount")
            if isinstance(free, (int, float)) and not isinstance(free, bool) and free > 0:
                # Deliberately not "N units", which the building-size reader
                # would take for the size of the whole building.
                detail.append(_homes_free_note(int(free)))
                metadata["homes_available"] = int(free)
        else:
            # A single home: the numbers are numbers here, not text.
            raw = home.get("unformattedPrice")
            price = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 else None
            beds = home.get("beds")
            if isinstance(beds, (int, float)) and not isinstance(beds, bool):
                metadata["bedrooms"] = int(beds)
                detail.append(f"Listed as {_bedroom_phrase(int(beds))}.")
            baths = home.get("baths")
            if isinstance(baths, (int, float)) and not isinstance(baths, bool) and baths > 0:
                plural = "" if baths == 1 else "s"
                detail.append(f"{int(baths) if float(baths).is_integer() else baths} bathroom{plural}.")
            area = home.get("area")
            if isinstance(area, (int, float)) and not isinstance(area, bool) and area > 0:
                metadata["floor_area"] = f"{int(area):,} sq ft"
                detail.append(f"Floor area {int(area):,} sq ft.")
            if price is not None:
                detail.append(f"Asking ${price:,} a month.")

        coordinates = home.get("latLong") if isinstance(home.get("latLong"), dict) else {}
        return ListingCandidate(
            platform=self.platform,
            source_id=identifier,
            title=name,
            original_url=url,
            price=price,
            neighborhood=(
                sf_area_from_address(street)
                or sf_target_coordinate_neighborhood(
                    coordinates.get("latitude"), coordinates.get("longitude")
                )
                or sf_area_from_zip(home.get("addressZipcode"))
            ),
            listing_type="Apartment building" if sizes else "Home",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # Zillow marks a room let inside a home on the unit itself.
            housing_kind=ROOM if is_room else WHOLE_UNIT,
        )

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        """Read the home's own page and say whether Zillow still lets it.

        Zillow's search goes on returning homes its own detail page has taken
        down. 502 Precita Ave and 161 Albion St were both arriving as
        ``FOR_RENT`` cards on the day their pages read "Off market"; three of
        eight active Zillow homes sampled from a real board were off the
        market, which is a fifth of that board sitting on the shortlist
        unrentable. Absence cannot catch them -- the search keeps returning
        them -- and this source could not read its own inventory either, so
        until now a Zillow home could only leave by ageing out after 21 days.

        The page is believed only when it says the same thing twice: nothing
        anywhere on it that Zillow marks ``FOR_RENT``, and a marker a reader
        would see for themselves. Measured over ten real pages the two never
        disagreed. A page that fails to render its data says neither, and is
        left meaning nothing -- an unread page must never cost a home its
        place.
        """
        response = client.get(listing.original_url, headers=_BROWSER_HEADERS, follow_redirects=True)
        # A listing Zillow has removed outright, stated as plainly as
        # Craigslist's 410 and read the same way.
        if response.status_code in (404, 410):
            return replace(
                listing,
                metadata={
                    **listing.metadata,
                    "zillow_page_checked": True,
                    "verified_inactive": True,
                    "verification_concern": (
                        f"Verified inactive: Zillow answered {response.status_code} for this home."
                    ),
                },
            )
        document = _require_page(response, self.platform)
        metadata = {**listing.metadata, "zillow_page_checked": True}
        if _ZILLOW_FOR_RENT.search(document):
            # Still let, and now said by the page rather than by a card. The
            # copy is new so the recheck records that the page was read; the
            # listing itself is unchanged.
            return replace(listing, metadata=metadata)
        if not _ZILLOW_OFF_MARKET.search(document):
            # Neither signal. The page answered but told us nothing we can
            # act on, so the home is left exactly as it was and tried again.
            return listing
        return replace(
            listing,
            metadata={
                **metadata,
                "verified_inactive": True,
                "verification_concern": (
                    "Verified inactive: Zillow's page for this home is off the market."
                ),
            },
        )


class UloopSource:
    """Read a university's off-campus housing board.

    Every other source here reads a commercial listing site. This is a student
    board, and the inventory is different in kind: rooms in shared flats,
    sublets, and small landlords who post where students look and nowhere
    else. It is also the only source whose cards all carry a real posting
    date, and it answers the monitor's own name rather than demanding a
    browser's.

    One board, not five. The five San Francisco schools -- UCSF, USF, SFSU, the
    Academy of Art and City College -- publish the *same* listings under
    per-board ids: "Parkmerced" is 2569628208 on one and 2569628209 on the
    next. Reading all five would be five times the requests for one board's
    inventory, and storing them by that numeric id would file one home five
    times. The slug is what is stable across boards, so that is the identity.

    There is no structured data on the page at all -- the only schema.org block
    is a breadcrumb -- so this is parsed out of the markup, which makes it the
    most fragile source here. It fails loudly on purpose.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "Uloop"
    mode = "automatic"
    # The per-school subdomain. Not uloop.com/housing/san-francisco-ca/, which
    # despite its name returned twenty-three cards and not one of them in San
    # Francisco: Denver, Houston, Daytona Beach, Logan Utah.
    search_url = "https://ucsf.uloop.com/housing/index.php/available"
    manual_reason = None
    # The card already carries price, size, posting date and a description.
    detail_budget = 0
    empty_result_message = "The student board currently lists no San Francisco homes."
    # San Francisco homes are scattered rather than clustered, so the board
    # has to be read further than the first cluster: five pages found 28,
    # twelve found 51. The walk stops on its own at a page that adds nothing.
    max_pages = 12
    # The board is asked for itself, with no filter in the request -- the
    # bedroom floor is applied to the cards below -- and the walk stops at the
    # page that adds nothing, which is the end of the board. Lowered below for
    # the walk that ends some other way, which the log already described.
    search_covers_inventory = True

    CARD = "div.listing-list.housing-listing"

    def _page_url(self, page: int) -> str:
        return self.search_url if page == 1 else f"{self.search_url}?page={page}"

    @staticmethod
    def _slug(url: str) -> str | None:
        """The identity a listing keeps across every board that carries it."""
        match = re.search(r"/housing/view\.php/\d+/([^/?#]+)", str(url or ""))
        return match.group(1) if match else None

    @staticmethod
    def _posted(text: str) -> str | None:
        """The board states a real posting date, unlike almost everything here."""
        match = re.search(r"\b(\d{2})/(\d{2})/(\d{2})\b", text)
        if not match:
            return None
        month, day, year = (int(part) for part in match.groups())
        try:
            return datetime(2000 + year, month, day, tzinfo=UTC).isoformat()
        except ValueError:
            return None

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        read_a_card = False
        document = ""
        # Claimed afresh every walk, and given up the moment this one stops
        # somewhere other than the end of the board.
        self.search_covers_inventory = True

        for page in range(1, self.max_pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(client.get(self._page_url(page)), self.platform)
            except (SourceError, httpx.TransportError):
                if read_a_card:
                    # Turned away part way: the pages after this one hold homes
                    # nobody looked at.
                    self.search_covers_inventory = False
                    break
                raise
            cards = BeautifulSoup(document, "html.parser").select(self.CARD)
            if not cards:
                break
            fresh = 0
            for card in cards:
                slug = self._slug((card.find("a", href=True) or {}).get("href", ""))
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                fresh += 1
                read_a_card = True
                candidate = self._candidate(card, slug, floor)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            if not fresh:
                break
        else:
            # Every page was read and every one still had new homes on it, so
            # the read ended at the limit rather than at the end of the board.
            # Said out loud, and now also said to the rule about absence: a
            # truncated result that says nothing reads exactly like complete
            # coverage, and the homes past page twelve are not missing.
            self.search_covers_inventory = False
            LOGGER.info(
                "%s stopped at its %s-page limit with homes still arriving; "
                "raise max_pages to see further.",
                self.platform,
                self.max_pages,
            )

        if not read_a_card:
            # A selector that has stopped matching must not read as a city with
            # no homes in it. This is markup, not a contract, and the day it
            # changes the scan has to say so rather than report a quiet market.
            raise _read_nothing(self.platform, document, "housing cards")
        return listings

    # "$2,892 - $5,216 · Studio, 1-3 Beds Apartment living at its best..." --
    # the figures and sizes are a fixed prefix, and everything after them is the
    # lister's own prose. Read from the whole card instead, a description that
    # mentions "utilities are about $50 per month" becomes the rent.
    _RENT = r"^\s*\$(?P<low>[\d,]+)(?:\s*-\s*\$(?P<high>[\d,]+))?\s*·\s*"
    # Two shapes, tried in order. "$1,350 · 3 Beds" and "$2,247 · Studio, 1-5
    # Beds" end at the word Beds; "$3,950 · Studio Space Modern Glen Park..."
    # never reaches it, and anchoring only on Beds loses the studio entirely --
    # its price and its size both.
    OFFERS = (
        re.compile(_RENT + r"(?P<sizes>[^·]*?)\s*Beds?\b", re.IGNORECASE),
        re.compile(_RENT + r"(?P<sizes>Studio)\b", re.IGNORECASE),
    )

    @classmethod
    def _offer(cls, card) -> tuple[int | None, tuple[int, int] | None]:
        """The asking rent and the bedroom sizes, from the card's own prefix."""
        node = card.select_one(".description")
        if node is None:
            return None, None
        said = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        match = next((found for found in (rule.match(said) for rule in cls.OFFERS) if found), None)
        if match is None:
            return None, None
        price = int(match.group("low").replace(",", ""))
        sizes = match.group("sizes")
        span = _bedroom_span(sizes)
        # "Studio, 1-3 Beds" offers a studio as well, which is a nought the
        # digits alone do not contain.
        if re.search(r"\bstudio\b", sizes, re.IGNORECASE):
            span = (0, span[1]) if span else (0, 0)
        return (price if price > 0 else None), span

    def _candidate(self, card, slug: str, floor: int) -> ListingCandidate | None:
        link = card.find("a", href=True)
        url = str(link["href"]).strip() if link else ""
        if not url.startswith("https://") or "uloop.com/housing/view.php/" not in url:
            return None

        titles = [
            node for node in card.select(".listingOneLineTitle")
            if "sub-title" not in (node.get("class") or [])
        ]
        title = _clean_text(titles[0].get_text(" ", strip=True), 180) if titles else ""
        located = card.select_one(".sub-title")
        address = _clean_text(located.get_text(" ", strip=True), 200) if located else ""
        # "19th Ave, San Francisco, CA, 94101": the city is the field before the
        # state, and the board writes it "San francisco" and "BERKELEY" as
        # readily as it writes it properly.
        city = re.search(r",\s*([A-Za-z .'-]+),\s*[A-Za-z]{2}\b", address)
        if not _is_san_francisco_locality(city.group(1) if city else ""):
            return None

        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        price, span = self._offer(card)
        if span is not None and span[1] < floor:
            return None

        street = address.split(",")[0].strip()
        metadata: dict[str, object] = {}
        if street:
            metadata["address"] = street
        posted = self._posted(text)
        if posted:
            # Genuinely when the home was posted, so it belongs here: it is what
            # the shortlist shows as "Posted" and sorts newest by.
            metadata["listing_timestamp"] = posted

        detail = [f"{title or street} on the student housing board."]
        if address:
            detail.append(f"Address: {address}.")

        if span is not None:
            low, high = span
            representative = max(low, min(floor, high))
            # Said in the summary, never asserted as metadata["bedrooms"]. On a
            # student board "$3,089 · 1 Bed" against "Furnished Master Bedroom"
            # means one room in a shared flat, not a one-bedroom home, and a
            # structured bedroom count would overrule the one reader that can
            # tell those apart. The number is still true enough to filter on.
            metadata["bedrooms_low"], metadata["bedrooms_high"] = low, high
            if low == high:
                detail.append(f"Listed as {_bedroom_phrase(low)}.")
            else:
                detail.append(f"It lets {_bedroom_noun(low)} through {_bedroom_noun(high)} homes.")
            # The board quotes one figure for a building that lets several
            # sizes, and it belongs to the smallest of them. Against a larger
            # home it would read as that home's rent.
            if price is not None and representative != low:
                metadata["price_from"] = price
                detail.append(_starting_rate_note(price, low, representative))
                price = None
        if price is not None:
            detail.append(f"Asking ${price:,} a month.")

        description = card.select_one(".desc")
        if description:
            said = _clean_text(description.get_text(" ", strip=True), 600)
            if said:
                detail.append(said)

        return ListingCandidate(
            platform=self.platform,
            # The slug, not the numeric id in the same URL: every board carries
            # the same homes under ids of its own, so the id would file one
            # home once per board.
            source_id=slug,
            title=title or street or slug.replace("-", " "),
            original_url=url,
            price=price,
            neighborhood=sf_area_from_address(street) or visible_sf_area_hint(f"{title} {address}"),
            listing_type=None,
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # Half of this board is rooms and half is whole homes, and the card
            # says which in words. Asserting either here would overrule the one
            # reader that can tell them apart.
        )

class RentSFNowSource:
    """Read the leasing feed of San Francisco's largest landlord.

    Veritas lets roughly 6,500 apartments across 293 buildings, and unlike the
    portals this is not a tower catalogue: these are older, mid-size, largely
    rent-controlled buildings, and the landlord states the neighbourhood itself
    rather than leaving it to be inferred from an address.

    Nothing is on the page. The San Francisco search returns eighty-five
    kilobytes of filter UI and no homes, and the sitemap enumerates four
    thousand property pages of which nearly all are long gone -- the most
    recently touched one reads "Apartment No Longer Available". What answers is
    the search plugin's own endpoint, which returns JSON to an ordinary request
    with no nonce, no cookie and no browser.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "RentSFNow"
    mode = "automatic"
    # What a reader opens. The homes come from the endpoint below, but this is
    # the page a person can actually look at, and it is what the dashboard
    # links and the Ready Check probes.
    search_url = "https://www.rentsfnow.com/apartments/sf/"
    ajax_url = "https://www.rentsfnow.com/wp-admin/admin-ajax.php"
    origin = "https://www.rentsfnow.com"
    manual_reason = None
    # The unit record is complete: address, area, size, baths, rent and pets.
    detail_budget = 0
    empty_result_message = "Veritas currently lets no San Francisco homes matching this deal."
    # Five pages of twelve today, and twenty pages found no more than eight
    # did: this source is already read in full. The cap is a backstop; the
    # server's own last_page and a page that adds nothing new stop it first.
    max_pages = 12

    def _request(self, page: int, floor: int) -> dict[str, str]:
        """The search form as the page itself submits it.

        Deliberately without latN/latS/lonE/lonW: those are the map view's
        viewport bounds, and sending them would quietly narrow a city-wide
        search to whatever rectangle happened to be on screen.
        """
        return {
            "neighborhood": "",
            "city": "san-francisco",
            "bedrooms": str(floor) if floor else "",
            "bathrooms": "",
            "sort": "priority_value-desc",
            "view": "list",
            "action": "wpas_ajax_load",
            "type": "json",
            "page": str(page),
        }

    @staticmethod
    def _criteria(value: object) -> tuple[tuple[int, int] | None, float | None, int | None]:
        """Size, bathrooms and rent, out of one escaped string.

        The feed writes ``"Studio \\\\ 1  Bath \\\\ &#36;1,945"``: HTML-escaped,
        with the parts divided by backslashes. All four shapes it uses are
        here -- Studio, N Bed, N Beds, and Baths plural or not.
        """
        parts = [part.strip() for part in re.split(r"\\+", unescape(str(value or ""))) if part.strip()]
        span = baths = price = None
        for part in parts:
            if re.search(r"\bstudio\b", part, re.IGNORECASE):
                span = (0, 0)
            elif re.search(r"\bbeds?\b", part, re.IGNORECASE):
                span = _bedroom_span(part)
            elif re.search(r"\bbaths?\b", part, re.IGNORECASE):
                found = re.search(r"([\d.]+)", part)
                baths = float(found.group(1)) if found else None
            elif "$" in part:
                # The dollar sign is the guard; requiring one again inside
                # _parse_price could never change the answer, and a test could
                # not tell the difference.
                price = _parse_price(part)
        return span, baths, price

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        answered = False
        last_page = self.max_pages

        for page in range(1, self.max_pages + 1):
            # PARTIAL_WALK: keep what the walk already read. See the note above _require_page.
            try:
                document = _require_page(
                    client.post(self.ajax_url, data=self._request(page, floor)), self.platform
                )
            except (SourceError, httpx.TransportError):
                if answered:
                    break
                raise
            try:
                payload = json.loads(document)
            except (ValueError, json.JSONDecodeError) as exc:
                raise SourceError("RentSFNow answered its search endpoint with something other than JSON.") from exc
            if not isinstance(payload, dict):
                raise SourceError("RentSFNow's search endpoint returned an unexpected shape.")
            answered = True
            # `units` only. Asked for something it does not have, the site
            # answers with recommended_units instead -- twelve homes that match
            # nothing that was asked for -- and its own page shows those in
            # place of results. Stored, they would be twelve inventions.
            units = payload.get("units")
            if not isinstance(units, list) or not units:
                break
            try:
                last_page = max(1, int(payload.get("last_page") or self.max_pages))
            except (TypeError, ValueError):
                last_page = self.max_pages

            fresh = 0
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                identifier = str(unit.get("id") or "").strip()
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                fresh += 1
                candidate = self._candidate(unit, identifier, floor)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings
            if not fresh or page >= last_page:
                break

        if not answered:
            raise SourceError("RentSFNow's search endpoint returned nothing at all.")
        return listings

    def _candidate(self, unit: dict, identifier: str, floor: int) -> ListingCandidate | None:
        path = str(unit.get("url") or "").strip()
        if not path:
            return None
        url = urljoin(self.origin, path)
        if not url.startswith(f"{self.origin}/"):
            return None
        title = _clean_text(unit.get("post_title"), 180)
        if not title:
            return None
        if str(unit.get("active", 1)) in {"0", "False", "false"}:
            return None

        span, baths, price = self._criteria(unit.get("criteria"))
        if span is not None and span[1] < floor:
            return None

        # The street, without the unit number, which is what the address table
        # can answer for.
        street = re.sub(r"\s*#.*$", "", title).strip()
        said = _clean_text(unit.get("neightborhood"), 60)
        metadata: dict[str, object] = {"address": street}
        detail = [f"{title} from RentSFNow."]
        if said:
            detail.append(f"{said}, as the landlord describes it.")

        if span is not None:
            metadata["bedrooms"] = span[0]
            detail.append(f"Listed as {_bedroom_phrase(span[0])}.")
        if baths is not None:
            metadata["bathrooms"] = baths
        if price:
            detail.append(f"Asking ${price:,} a month.")
        if unit.get("is_furnished"):
            metadata["furnished"] = True
            detail.append("Furnished.")
        if str(unit.get("iscomingsoon") or "").casefold() == "yes":
            metadata["coming_soon"] = True
            detail.append("Listed as coming soon rather than available now.")
        pets = _clean_text(unit.get("pet"), 40)
        if pets:
            detail.append(f"Pets: {pets}.")

        return ListingCandidate(
            platform=self.platform,
            # The feed's own post id. The slug carries the address, so a
            # renumbered unit would arrive as a second home.
            source_id=identifier,
            title=title,
            original_url=url,
            price=price,
            # The landlord's own word first, where it is a name this deal can
            # rank; then the street. No table of near-misses: "Lower Nob Hill"
            # becomes Nob Hill because the address says so, not because
            # somebody decided the two are the same.
            neighborhood=(
                said if said in SF_NEIGHBORHOODS else visible_sf_area_hint(said) or sf_area_from_address(street)
            ),
            listing_type="Apartment",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            # Veritas lets whole apartments, never a room in one.
            housing_kind=WHOLE_UNIT,
        )

class AvalonBaySource:
    """Read AvalonBay's San Francisco page, which ships its own data.

    The page renders server-side and carries the whole result set as a JSON
    blob assigned to ``Fusion.globalContent`` -- unit by unit, not building by
    building, with a floor, a square footage, a real move-in date and a rent.
    That is richer than anything here except Rent.com, and it costs one request.

    Eight of the sixteen buildings the page lists are Equity Residential stock
    AvalonBay markets, and they were worth writing a line about until the feed
    was counted: they contribute no units at all. The unit list is AvalonBay's
    own, and the eight are already in this database through Redfin and
    Rent.com. The buildings are still read, for the link each unit needs.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "AvalonBay"
    # AvalonBay ships its entire unit list inside its own San Francisco page,
    # one request and no paging. The bedroom floor is applied to that list
    # here, not asked of AvalonBay, so the list does not change with the deal.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://www.avaloncommunities.com/california/san-francisco-apartments/"
    manual_reason = None
    # Every unit arrives complete. There is nothing a detail page would add.
    detail_budget = 0
    empty_result_message = "AvalonBay lists no San Francisco homes matching this deal."

    BLOB = re.compile(r"Fusion\.globalContent\s*=\s*(\{.*?\});", re.S)

    @classmethod
    def _payload(cls, document: str) -> dict:
        match = cls.BLOB.search(document)
        if not match:
            raise _read_nothing(cls.platform, document, "embedded search results")
        try:
            parsed = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceError("AvalonBay's embedded results are no longer valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise SourceError("AvalonBay's embedded results are not the expected shape.")
        return parsed

    def _unit_url(self, community: dict | None, identifier: str) -> str:
        """A link per home, because one link per source stores one home.

        ``listings.canonical_url`` is UNIQUE, so pointing every unit at the
        search page would file the first one and silently discard the rest. The
        building's page carries the unit as a query parameter, which is the
        shape the city portal already uses for the same reason -- and which
        ``canonicalize_url`` keeps while dropping the campaign tags AvalonBay
        hangs off its Equity Residential links.
        """
        page = str((community or {}).get("url") or "").strip()
        base = urljoin(self.search_url, page) if page else self.search_url
        return f"{base}{'&' if '?' in base else '?'}unit={quote(identifier, safe='')}"

    @staticmethod
    def _communities(payload: dict) -> dict[str, dict]:
        """Buildings by id, so a unit can borrow its page and its operator.

        A unit carries no link of its own and does not say who runs the
        building; the building carries both.
        """
        block = payload.get("communityResults")
        block = block.get("communities") if isinstance(block, dict) else None
        items = block.get("items") if isinstance(block, dict) else None
        return {
            str(item.get("communityId")): item
            for item in (items or [])
            if isinstance(item, dict) and item.get("communityId")
        }

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        payload = self._payload(_require_page(client.get(self.search_url), self.platform))
        units = (payload.get("unitResults") or {}).get("items")
        if not isinstance(units, list):
            raise SourceError("AvalonBay published no unit list in its embedded results.")
        communities = self._communities(payload)

        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        for unit in units:
            if not isinstance(unit, dict):
                continue
            identifier = str(unit.get("unitId") or "").strip()
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            candidate = self._candidate(unit, identifier, communities, floor)
            if candidate is not None:
                listings.append(candidate)
                if len(listings) >= maximum:
                    break
        return listings

    def _candidate(
        self, unit: dict, identifier: str, communities: dict[str, dict], floor: int
    ) -> ListingCandidate | None:
        address = unit.get("address") if isinstance(unit.get("address"), dict) else {}
        # The page is titled San Francisco and answers with San Bruno and
        # Pacifica in it -- twenty-seven and ten of a hundred and thirty-seven.
        if not _is_san_francisco_locality(address.get("city")):
            return None

        bedrooms = unit.get("bedroomNumber")
        if not isinstance(bedrooms, (int, float)) or isinstance(bedrooms, bool):
            return None
        bedrooms = int(bedrooms)
        if bedrooms < floor:
            return None

        # The unfurnished price and date, deliberately. A furnished quote is a
        # different product at a different rent, and "OnDemand" means furnishing
        # is offered rather than that the home comes furnished.
        offer = unit.get("startingAtPricesUnfurnished")
        prices = offer.get("prices") if isinstance(offer, dict) else None
        raw_price = prices.get("price") if isinstance(prices, dict) else None
        price = int(raw_price) if isinstance(raw_price, (int, float)) and raw_price > 0 else None

        community = communities.get(str(unit.get("communityId") or ""))
        street = _clean_text(address.get("addressLine1"), 160)
        name = _clean_text(unit.get("communityName"), 120)
        title = f"{name} — {street}" if name and street else name or street
        if not title:
            return None

        metadata: dict[str, object] = {"address": street, "building_listing": True}
        detail = [f"{title} from AvalonBay."]
        if bedrooms is not None:
            metadata["bedrooms"] = bedrooms
            detail.append(f"Listed as {_bedroom_phrase(bedrooms)}.")
        baths = unit.get("bathroomNumber")
        if isinstance(baths, (int, float)) and not isinstance(baths, bool):
            metadata["bathrooms"] = float(baths)
        size = unit.get("squareFeet")
        if isinstance(size, (int, float)) and size > 0:
            metadata["floor_area"] = f"{int(size)} sq ft"
            detail.append(f"{int(size)} sq ft.")
        floor_number = _clean_text(unit.get("floorNumber"), 8)
        if floor_number:
            detail.append(f"Floor {floor_number}.")
        if price:
            detail.append(f"Asking ${price:,} a month.")

        stated = _american_date(str(unit.get("availableDateUnfurnished") or "")[:10])
        if stated:
            # Written the way the scoring reads a move-in date; an ISO date
            # under a key of its own is a fact nothing consults.
            metadata["available_on"] = stated
            detail.append(f"Available {stated}.")
        return ListingCandidate(
            platform=self.platform,
            source_id=identifier,
            title=title,
            original_url=self._unit_url(community, identifier),
            price=price,
            # Street, then ZIP. The buildings' own map pins were tried and
            # dropped: measured over all hundred San Francisco units they
            # placed none of them, because the coordinate boxes this app keeps
            # are a small hand-picked set and no AvalonBay building sits in
            # one. A line that looks like coverage and provides none is worse
            # than the gap it hides. The join stays for the link and the
            # operator, which it does earn.
            neighborhood=sf_area_from_address(street) or sf_area_from_zip(address.get("zip")),
            listing_type="Apartment",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            housing_kind=WHOLE_UNIT,
        )

class AppFolioSource:
    """Read the small San Francisco managers who let through AppFolio.

    One parser, a list of managers. Every tenant site is the same page at
    ``https://<subdomain>.appfolio.com/listings`` with the same markup, so
    adding a manager is a row in ``data/appfolio_managers.json`` and no code at
    all. This is the fifty-buildings idea in the shape that works: not fifty
    scrapers, one scraper and a list.

    These are small landlords -- six homes between three managers on the day
    this was written -- but they are older buildings let by people who post
    where their own tenants look, and none of it reaches a portal.

    The failure that matters is a subdomain which is not a tenant site. AppFolio
    answers those with HTTP 200 and its own "Page not found", which parses into
    zero homes and would read as a manager with nothing free, for good. A real
    tenant site always carries the listings container, whether or not it has
    anything in it, so that is what tells them apart.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "AppFolio"
    # Every manager on the list is asked for its whole listings page, and a
    # manager that did not answer is skipped rather than read as empty -- with
    # none of them answering the search fails outright. So the homes that come
    # back are the homes these managers are advertising.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://chandlerproperties.appfolio.com/listings"
    manual_reason = None
    # Everything is on the one page: rent, size, square footage, availability
    # and the full address.
    detail_budget = 0
    empty_result_message = "The AppFolio managers list no San Francisco homes today."

    CARD = "div.listing-item"
    # Present on every tenant site, with or without listings on it. Absent from
    # AppFolio's own not-found page, which is the whole point.
    CONTAINER = "js-listings"
    ROSTER = Path(__file__).resolve().parent / "data" / "appfolio_managers.json"

    @classmethod
    def managers(cls) -> list[dict[str, str]]:
        try:
            roster = json.loads(cls.ROSTER.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SourceError("The AppFolio manager list could not be read.") from exc
        managers = roster.get("managers") if isinstance(roster, dict) else None
        if not isinstance(managers, list) or not managers:
            raise SourceError("The AppFolio manager list is empty.")
        return [
            {"subdomain": str(m["subdomain"]), "name": str(m.get("name") or m["subdomain"])}
            for m in managers
            if isinstance(m, dict) and m.get("subdomain")
        ]

    @staticmethod
    def origin(subdomain: str) -> str:
        return f"https://{subdomain}.appfolio.com"

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        listings: list[ListingCandidate] = []
        answered = 0
        managers = self.managers()

        for manager in managers:
            origin = self.origin(manager["subdomain"])
            try:
                document = _require_page(client.get(f"{origin}/listings"), self.platform)
            except SourceError as exc:
                # One manager's outage is not the end of the others.
                LOGGER.info("%s could not read %s: %s", self.platform, manager["name"], exc)
                continue
            if self.CONTAINER not in document:
                # Not a tenant site. Counted as nothing rather than as a
                # manager with nothing, because the two look identical and only
                # one of them is somebody's typo.
                LOGGER.warning(
                    "%s: %s is not an AppFolio tenant site; check the subdomain %r.",
                    self.platform, manager["name"], manager["subdomain"],
                )
                continue
            answered += 1
            for card in BeautifulSoup(document, "html.parser").select(self.CARD):
                candidate = self._candidate(card, manager, origin, floor)
                if candidate is not None:
                    listings.append(candidate)
                    if len(listings) >= maximum:
                        return listings

        if not answered:
            raise SourceError(
                f"None of the {len(managers)} AppFolio managers answered with a listings page."
            )
        return listings

    # "1 bd / 1 ba", "Studio / 1 ba", "3 bd / 2.5 ba". Both numbers live in one
    # string, so reading it as a bedroom range makes a two-bed-one-bath into a
    # one-bedroom and a three-bed-two-and-a-half into a two. The bed count is
    # the figure before "bd", and a studio says so in words instead.
    BED_BATH = re.compile(r"(?:(?P<studio>studio)|(?P<beds>[\d.]+)\s*bd)?[^\d]*(?:(?P<baths>[\d.]+)\s*ba)?", re.IGNORECASE)

    @classmethod
    def _bed_bath(cls, value: str) -> tuple[int | None, float | None]:
        match = cls.BED_BATH.search(value or "")
        if match is None:
            return None, None
        if match.group("studio"):
            bedrooms: int | None = 0
        elif match.group("beds"):
            try:
                bedrooms = int(float(match.group("beds")))
            except ValueError:
                bedrooms = None
        else:
            bedrooms = None
        try:
            baths = float(match.group("baths")) if match.group("baths") else None
        except ValueError:
            baths = None
        return bedrooms, baths

    @staticmethod
    def _text(card, selector: str) -> str:
        node = card.select_one(selector)
        return _clean_text(node.get_text(" ", strip=True), 200) if node else ""

    def _candidate(self, card, manager: dict, origin: str, floor: int) -> ListingCandidate | None:
        link = card.select_one("a[href]")
        href = str(link["href"]).strip() if link else ""
        # The uuid is the identity. The rest of the path carries the address.
        found = re.search(r"/listings/detail/([A-Za-z0-9-]+)", href)
        if not found:
            return None
        url = urljoin(origin, href)

        address = self._text(card, ".js-listing-address")
        # "1330 Jones St. Apt 306, San Francisco, CA 94109" -- the city is the
        # field before the state.
        city = re.search(r",\s*([A-Za-z .'-]+),\s*[A-Za-z]{2}\b", address)
        if not _is_san_francisco_locality(city.group(1) if city else ""):
            return None

        bedrooms, baths = self._bed_bath(self._text(card, ".js-listing-blurb-bed-bath"))
        if bedrooms is not None and bedrooms < floor:
            return None
        price = _parse_price(self._text(card, ".js-listing-blurb-rent"), require_currency=True)
        title = self._text(card, ".js-listing-title") or address.split(",")[0]
        street = address.split(",")[0].strip()

        metadata: dict[str, object] = {"address": street, "small_manager": True}
        detail = [f"{title} — let by {manager['name']} through AppFolio."]
        if address:
            detail.append(f"Address: {address}.")
        if bedrooms is not None:
            metadata["bedrooms"] = bedrooms
            detail.append(f"Listed as {_bedroom_phrase(bedrooms)}.")
        if baths is not None:
            metadata["bathrooms"] = baths
        for item in card.select(".detail-box__item"):
            label = self._text(item, ".detail-box__label").casefold()
            value = self._text(item, ".detail-box__value")
            if label.startswith("square") and value.isdigit():
                metadata["floor_area"] = f"{value} sq ft"
                detail.append(f"{value} sq ft.")
        available = self._text(card, ".js-listing-available")
        if available:
            stated = _american_date(available) or (
                "now" if available.strip().casefold() in {"now", "available now"} else ""
            )
            if stated and stated != "now":
                metadata["available_on"] = stated
            detail.append(f"Available {available.lower()}.")
        if price:
            detail.append(f"Asking ${price:,} a month.")

        return ListingCandidate(
            platform=self.platform,
            source_id=found.group(1),
            title=title,
            original_url=url,
            price=price,
            neighborhood=sf_area_from_address(street),
            listing_type="Apartment",
            summary=_clean_text(" ".join(detail), 1200),
            metadata=metadata,
            housing_kind=WHOLE_UNIT,
        )

class UDRSource:
    """Read UDR's San Francisco page, which prices a building by bedroom size.

    Six buildings, each publishing a starting rent per size rather than a rent
    per home. So one candidate per building and size, following the city
    portal's precedent: a building offering a studio and a one-bedroom is two
    things a reader can judge separately, and collapsing them to a single
    "from" price would hide the one they actually want.

    A size with nothing free reads ``"0 Available Apartments"`` where the rent
    goes. Parsed as a number that would be a home going for nothing, so a rent
    has to carry a dollar sign to count, and a size without one is not offered
    at all rather than offered at an unknown price -- UDR is saying it has none.
    """
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True

    platform = "UDR"
    # UDR's San Francisco page carries every building it lets here, in one
    # request. As with AvalonBay, the bedroom floor is applied to the cards
    # rather than to the request.
    search_covers_inventory = True
    mode = "automatic"
    search_url = "https://www.udr.com/san-francisco-bay-area-apartments/san-francisco/"
    origin = "https://www.udr.com"
    manual_reason = None
    detail_budget = 0
    empty_result_message = "UDR lists no San Francisco homes matching this deal."

    CARD = ".community-card__container"
    SIZES = {"studio": 0, "1 bedroom": 1, "2 bedrooms": 2, "3 bedrooms": 3, "4 bedrooms": 4}

    @classmethod
    def _offers(cls, card) -> list[tuple[int, int]]:
        """Every bedroom size this building has a real rent for."""
        cells = [node.get_text(" ", strip=True) for node in card.select(".community-card__rent-cell")]
        offers = []
        for label, rent in zip(cells, cells[1:]):
            size = cls.SIZES.get(_normal_text(label))
            if size is None:
                continue
            # A rent has to be money. Without that, "3 Available Apartments" --
            # a size that has some free and publishes no rent -- reads as a
            # home going for three dollars a month, and "0 Available
            # Apartments" is only caught by nought being falsy, which is luck.
            price = _parse_price(rent, require_currency=True)
            if price:
                offers.append((size, price))
        return offers

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        floor = _bedroom_floor(preferences)
        maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        document = _require_page(client.get(self.search_url), self.platform)
        cards = BeautifulSoup(document, "html.parser").select(self.CARD)
        if not cards:
            raise _read_nothing(self.platform, document, "building cards")

        listings: list[ListingCandidate] = []
        seen: set[str] = set()
        for card in cards:
            for candidate in self._candidates(card, floor):
                if candidate.source_id in seen:
                    continue
                seen.add(candidate.source_id)
                listings.append(candidate)
                if len(listings) >= maximum:
                    return listings
        return listings

    # A building's link may end in a marketing sub-page: /399-fremont/specials/
    # and /2000-post/specials/ both end in "specials", so taking the last
    # segment named five different buildings the same thing and four of them
    # were deduplicated away in silence.
    MARKETING_SEGMENTS = frozenset({"specials", "floorplans", "availability", "gallery", "amenities"})

    @classmethod
    def _building_slug(cls, page: str) -> str:
        parts = [part for part in urlsplit(page).path.strip("/").split("/") if part]
        while parts and parts[-1].casefold() in cls.MARKETING_SEGMENTS:
            parts.pop()
        return parts[-1] if parts else _source_id(page)

    @staticmethod
    def _one(card, selector: str) -> str:
        node = card.select_one(selector)
        return _clean_text(node.get_text(" ", strip=True), 160) if node else ""

    def _candidates(self, card, floor: int) -> list[ListingCandidate]:
        city = self._one(card, ".community-card__city-state")
        # "San Francisco, CA 94105" -- the city is what precedes the state.
        named = re.match(r"([A-Za-z .'-]+),", city)
        if not _is_san_francisco_locality(named.group(1) if named else ""):
            return []
        link = card.select_one("a[href]")
        href = str(link["href"]).strip() if link else ""
        if not href:
            return []
        page = urljoin(self.origin, href)
        slug = self._building_slug(page)
        name = self._one(card, ".community-card__title")
        street = self._one(card, ".community-card__number-street")
        if not name and not street:
            return []

        made = []
        for size, price in self._offers(card):
            if size < floor:
                continue
            detail = [f"{name or street} from UDR."]
            if street:
                detail.append(f"Address: {street}, {city}.")
            detail.append(f"Its {_bedroom_noun(size)} homes start at ${price:,} a month.")
            detail.append("A building's starting rent for this size, not one home's.")
            made.append(
                ListingCandidate(
                    platform=self.platform,
                    # One row per building and size. The city portal files a
                    # building's unit types the same way, and for the same
                    # reason: canonical_url is UNIQUE, so one row per building
                    # would keep whichever size happened to be read first.
                    source_id=f"{slug}:{size}",
                    title=f"{name or street} — {_bedroom_noun(size)}",
                    original_url=f"{page}{'&' if '?' in page else '?'}unit={size}",
                    price=price,
                    neighborhood=sf_area_from_address(street) or visible_sf_area_hint(name),
                    listing_type="Apartment building",
                    summary=_clean_text(" ".join(detail), 900),
                    metadata={"address": street, "bedrooms": size, "building_listing": True},
                    housing_kind=WHOLE_UNIT,
                )
            )
        return made

class ApifyFacebookMarketplaceSource:
    """Optional low-volume Facebook automation that does not use a FB login."""

    platform = "Facebook Marketplace"
    mode = "automatic"
    detail_budget = 0
    provider = "apify"
    connector_key = "apify"
    scheduled_only = True
    manual_scan_enabled = True
    # Measured on this actor: about $0.013 of Apify credit per listing read
    # with details. Apify's free tier is $5 a month, so a twice-daily scan can
    # afford roughly six listings a run and nothing like a full board. Ten was
    # not an arbitrary smallness; it was the free tier. Raising the ceiling is
    # a spending decision, so it lives in preferences rather than in a default.
    results_limit = 10
    # One deep read when the board is empty, then the cheap steady trickle.
    # At roughly $0.013 of credit a listing, the first scan is about $1.30 of
    # a $5 monthly tier and every scan after it about $0.13.
    first_scan_results_limit = 100
    max_results_per_scan = 250
    monthly_run_limit = 60
    request_timeout_seconds = 75.0
    actor_url = "https://api.apify.com/v2/acts/apify~facebook-marketplace-scraper/run-sync-get-dataset-items"
    search_url = "https://www.facebook.com/marketplace/114952118516947/propertyrentals/"
    manual_reason = None

    def __init__(self, tokens: ApifyTokenStore):
        self.tokens = tokens

    @staticmethod
    def _marketplace_url(preferences: Preferences) -> str:
        budget = preferences.section("budget")
        whole_unit = preferences.section("whole_unit")
        two_bedroom = preferences.section("two_bedroom")
        three_bedroom = preferences.section("three_bedroom")
        query: dict[str, str | int] = {
            # The normal keyword search leaks a large number of irrelevant Bay
            # Area products. The city-scoped Property Rentals feed is where
            # Marketplace's room listings, including the user's verified
            # examples, are actually published.
            "sortBy": "creation_time_descend",
            "radius": 10,
            "exact": "false",
        }
        if budget.get("min_monthly") is not None:
            query["minPrice"] = max(0, round(int(budget["min_monthly"]) * 0.95))
        if budget.get("max_monthly") is not None:
            maximum = int(budget["max_monthly"])
            if whole_unit.get("enabled", True) is True:
                maximum = max(maximum, setting_int(whole_unit.get("max_monthly"), 3000))
            if two_bedroom.get("enabled", True) is True:
                shared_total = setting_int(two_bedroom.get("occupants"), 2) * int(
                    two_bedroom.get("max_per_person", 2700)
                )
                maximum = max(maximum, shared_total)
            if three_bedroom.get("enabled", True) is True:
                shared_total = setting_int(three_bedroom.get("occupants"), 3) * int(
                    three_bedroom.get("max_per_person", 2500)
                )
                maximum = max(maximum, shared_total)
            query["maxPrice"] = round(maximum * 1.05)
        return (
            "https://www.facebook.com/marketplace/114952118516947/propertyrentals/?"
            + urlencode(query)
        )

    @classmethod
    def _start_urls(cls, preferences: Preferences) -> list[str]:
        """Every feed a rental actually appears in, read in one run.

        The city Property Rentals feed is where whole units are published, and
        it is the only one this asked for. Rooms and sublets are posted as
        ordinary Marketplace items and never reach that feed, so the board was
        missing a whole category rather than merely being short.
        """
        urls = [cls._marketplace_url(preferences)]
        paths = set(getattr(preferences.deal_profile, "enabled_paths", ()) or ())
        keywords: list[str] = []
        if "private_room" in paths:
            keywords.append("room for rent San Francisco")
        if paths.intersection({"studio", "one_bedroom", "two_bedroom", "three_bedroom"}):
            keywords.append("apartment for rent San Francisco")
        keywords.append("sublet San Francisco")
        for keyword in keywords:
            urls.append(
                "https://www.facebook.com/marketplace/114952118516947/search/?"
                + urlencode({"query": keyword, "sortBy": "creation_time_descend"})
            )
        return urls

    @staticmethod
    def _flatten(value, limit: int = 420) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return _clean_text(value, limit)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return _clean_text(" ".join(ApifyFacebookMarketplaceSource._flatten(item) for item in value), limit)
        if isinstance(value, dict):
            preferred = ["text", "display_name", "formatted_amount", "formatted", "name", "value"]
            ordered = [value[key] for key in preferred if key in value]
            if not ordered:
                ordered = list(value.values())
            return _clean_text(" ".join(ApifyFacebookMarketplaceSource._flatten(item) for item in ordered), limit)
        return ""

    @staticmethod
    def _price(item: dict) -> int | None:
        for field in ("listingPrice", "listing_price", "min_listing_price", "max_listing_price"):
            value = item.get(field)
            if value is None:
                continue
            formatted = ApifyFacebookMarketplaceSource._flatten(value)
            price = _parse_price(formatted, require_currency=True)
            if price is not None:
                return price
            if isinstance(value, dict):
                raw = value.get("amount", value.get("value"))
            else:
                raw = value
            try:
                numeric = float(str(raw).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if numeric > 10_000:
                numeric /= 100
            return round(numeric)
        return None

    @staticmethod
    def _direct_item_url(item: dict) -> str | None:
        for field in ("itemUrl", "listingUrl", "listing_url", "url"):
            match = re.search(r"https?://(?:www\.)?facebook\.com/marketplace/item/(\d+)", str(item.get(field, "")))
            if match:
                return f"https://www.facebook.com/marketplace/item/{match.group(1)}/"
        return None

    @classmethod
    def _location_text(cls, item: dict) -> str:
        """Prefer Facebook's structured reverse-geocode over a raw coordinate blob."""
        location = item.get("location")
        parts: list[str] = []
        if isinstance(location, dict):
            detailed = location.get("reverse_geocode_detailed")
            reverse = location.get("reverse_geocode")
            for value in (detailed, reverse, location):
                if isinstance(value, dict):
                    city = value.get("city")
                    state = value.get("state")
                    postal = value.get("postal_code")
                    if city:
                        parts.append(str(city))
                    if state:
                        parts.append(str(state))
                    if postal:
                        parts.append(str(postal))
            latitude, longitude = location.get("latitude"), location.get("longitude")
            if latitude is not None and longitude is not None:
                parts.append(f"{latitude} {longitude}")
        raw = cls._flatten(item.get("locationText"))
        if raw:
            parts.append(raw)
        return _clean_text(" ".join(dict.fromkeys(parts)), 180)

    @classmethod
    def _detail_text(cls, item: dict) -> str:
        """Flatten Property Rentals' structured facts into scoreable, user-visible text."""
        return cls._flatten(item.get("details"), 1_400)

    @staticmethod
    def _is_san_francisco(location: str) -> bool:
        """Keep the city feed honest when Marketplace leaks nearby Bay Area cards."""
        text = _normal_text(location)
        if "san francisco" in text:
            return True
        # A source-provided city beats a nearby latitude/longitude. The south
        # city boundary is close enough to Daly City that coordinate-only
        # containment cannot safely distinguish every address.
        nearby_cities = ("daly city", "brisbane", "south san francisco", "pacifica", "colma")
        if any(city in text for city in nearby_cities):
            return False
        match = re.search(r"\b(37\.\d{4,})\s+(-122\.\d{4,})\b", location)
        if not match:
            return False
        latitude, longitude = (float(match.group(1)), float(match.group(2)))
        # City-level containment only; neighborhood ranking happens separately.
        return 37.7060 <= latitude <= 37.8325 and -122.5300 <= longitude <= -122.3500

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        """Normalize a completed actor dataset without issuing another actor run."""
        listings: list[ListingCandidate] = []
        actor_errors = 0
        # Where the rows went. A run that asks for 150 and shows 108 is either
        # over-filtering or under-fetching, and only a breakdown says which.
        dropped = {"malformed": 0, "sold": 0, "ended": 0, "unusable": 0, "outside_sf": 0}
        for item in rows:
            if not isinstance(item, dict):
                dropped["malformed"] += 1
                continue
            if item.get("error") or item.get("errorDescription"):
                actor_errors += 1
                continue
            if item.get("is_sold") is True or item.get("isSold") is True:
                dropped["sold"] += 1
                continue
            if item.get("is_live") is False or item.get("isLive") is False:
                dropped["ended"] += 1
                continue
            original_url = self._direct_item_url(item)
            title = _clean_text(
                str(
                    item.get("listingTitle")
                    or item.get("marketplace_listing_title")
                    or item.get("custom_title")
                    or item.get("customTitle")
                    or ""
                ),
                180,
            )
            if not original_url or not title:
                dropped["unusable"] += 1
                continue
            description = self._flatten(item.get("description"), 1_200)
            location = self._location_text(item)
            detail_text = self._detail_text(item)
            if not self._is_san_francisco(location):
                # The Facebook category's radius is advisory, not a guaranteed
                # boundary. Do not clutter the local archive with Daly City,
                # Richmond, Oakland, or San Rafael cards.
                dropped["outside_sf"] += 1
                continue
            context = " ".join(part for part in (location, title, description, detail_text) if part)
            coordinate_neighborhood = facebook_coordinate_neighborhood(location)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=_source_id(original_url),
                    title=title,
                    original_url=original_url,
                    price=self._price(item),
                    neighborhood=(
                        GmailHousingAlertSource._neighborhood(context, preferences)
                        or coordinate_neighborhood
                        or location
                        or None
                    ),
                    listing_type="Marketplace rental",
                    summary=_clean_text(" ".join(part for part in (description, detail_text) if part), 1_400)
                    or _clean_text(context, 420),
                    metadata={
                        "apify_actor": "apify/facebook-marketplace-scraper",
                        "listing_timestamp": item.get("timestamp"),
                    },
                )
            )
        LOGGER.info(
            "Facebook Marketplace: %s rows in, %s kept, dropped %s, actor errors %s",
            len(rows),
            len(listings),
            ", ".join(f"{name} {count}" for name, count in dropped.items() if count) or "none",
            actor_errors,
        )
        if not listings:
            detail = f" ({actor_errors} actor error row(s))" if actor_errors else ""
            raise SourceError(f"The Facebook helper returned no parseable active San Francisco listing links{detail}.")
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        if not self.tokens.reserve_monthly_run(limit=self.monthly_run_limit):
            raise SourceError(
                "The local 60-run monthly Facebook safety cap was reached (or its usage file is invalid)."
            )
        self.search_url = self._marketplace_url(preferences)
        start_urls = self._start_urls(preferences)
        first_scan = self.tokens.first_scan_pending("marketplace")
        whole_unit = preferences.section("whole_unit")
        results_limit = self.results_limit
        if whole_unit.get("enabled", True) is True:
            try:
                results_limit = int(
                    preferences.section("sources").get(
                        "facebook_marketplace_results_per_scan", self.results_limit
                    )
                )
            except (TypeError, ValueError) as exc:
                raise SourceError("The Facebook result limit must be a whole number.") from exc
            results_limit = max(self.results_limit, min(results_limit, self.max_results_per_scan))
        if first_scan:
            results_limit = max(results_limit, self.first_scan_results_limit)
        payload = {
            "startUrls": [{"url": url} for url in start_urls],
            # Ten newest detailed rental cards twice a day cover all housing
            # workflows while keeping this personal monitor deliberately small.
            "resultsLimit": results_limit,
            "includeListingDetails": True,
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError(
                f"The Facebook helper exceeded its {int(self.request_timeout_seconds)}-second time limit."
            ) from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the API token; reconnect it from Alerts.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no charge was attempted here.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Facebook helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Facebook helper returned an unexpected result format.")
        listings = self.parse_rows(rows, preferences)
        # Only once it has actually worked: a first scan that failed should
        # still get its deep read rather than quietly dropping to ten.
        if first_scan and listings:
            self.tokens.mark_first_scan_done("marketplace")
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FacebookMarketplaceSource:
    """Use native Marketplace alerts when present; otherwise retain the capped fallback."""

    platform = "Facebook Marketplace"
    detail_budget = 0
    manual_scan_enabled = True

    def __init__(self, mailbox: GmailAlertMailbox, tokens: ApifyTokenStore):
        self.alerts = FacebookMarketplaceAlertSource(mailbox)
        self.apify = ApifyFacebookMarketplaceSource(tokens)
        self.tokens = tokens
        self.last_provider = "gmail_alerts"

    @property
    def connector_key(self) -> str:
        return "gmail" if self.last_provider == "gmail_alerts" else "apify"

    @property
    def connector_state_key(self) -> str:
        return self.alerts.connector_state_key if self.last_provider == "gmail_alerts" else "apify"

    @property
    def last_alert_count(self) -> int:
        return self.alerts.last_alert_count if self.last_provider == "gmail_alerts" else 0

    @property
    def mode(self) -> str:
        if self.tokens.is_configured:
            return "automatic"
        return self.alerts.mode

    @property
    def scheduled_only(self) -> bool:
        return self.tokens.is_configured

    @property
    def search_url(self) -> str:
        return self.apify.search_url if self.tokens.is_configured else self.alerts.search_url

    @property
    def manual_reason(self) -> str:
        if self.tokens.is_configured:
            return ""
        if self.alerts.mode == "setup":
            return "Connect the capped Apify free-tier helper (recommended) or Gmail alert fallback from Alerts."
        return self.alerts.manual_reason

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        # Native saved-search email carries the user's exact Marketplace search
        # and does not consume a third-party scraping run. Keep Apify available
        # until actual direct-link alert emails arrive, since a newly connected
        # Gmail account may legitimately have no Facebook alerts yet.
        if self.alerts.mode == "automatic":
            try:
                native_listings = self.alerts.search(client, preferences)
            except SourceError:
                native_listings = []
            if native_listings:
                self.last_provider = "gmail_alerts"
                return native_listings
        if self.tokens.is_configured:
            self.last_provider = "apify"
            return self.apify.search(client, preferences)
        self.last_provider = "gmail_alerts"
        return self.alerts.search(client, preferences)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FacebookGroupsSource:
    """Monitor configured *public* housing groups with a capped Apify actor call."""

    platform = "Facebook Groups"
    provider = "apify"
    connector_key = "apify"
    # Its own row: writing group results into the Marketplace connector made a
    # group test overwrite the Marketplace answer and report nothing of its own.
    connector_state_key = "facebook_groups"
    detail_budget = 0
    scheduled_only = True
    manual_scan_enabled = True
    # About $0.016 of credit per post, on the same $5 monthly free tier, so
    # groups get the same shape: one deep read to fill the board, then enough
    # to catch what is new.
    results_limit = 10
    first_scan_results_limit = 50
    manual_results_limit = 30
    monthly_post_limit = 400
    request_timeout_seconds = 75.0
    actor_url = "https://api.apify.com/v2/acts/apify~facebook-groups-scraper/run-sync-get-dataset-items"

    _HOUSING_TERMS = re.compile(
        r"\b(?:room|bedroom|sublet|sublease|lease\s*takeover|roommate|housing|rental|rent)\b",
        re.IGNORECASE,
    )
    _STRONG_OFFER_TERMS = re.compile(
        r"(?:"
        r"^[^a-z0-9]{0,12}(?:private\s+)?(?:room|bedroom|studio|apartment|flat|house|unit)\b[^.!?]{0,40}\b(?:available|for\s+rent)\b|"
        r"\b(?:i|we)\s+have\s+(?:a\s+)?(?:private\s+)?(?:room|bedroom|studio|apartment|flat|house|unit)\b|"
        r"\b(?:my|our)\s+(?:room|bedroom|studio|apartment|flat|house|unit|lease)\b[^.!?]{0,60}\b(?:available|rent|sublet|sublease|takeover)\b|"
        r"\brenting\s+out\b|\broommate\s+(?:wanted|needed)\b|"
        r"\blooking\s+for\s+(?:someone|a\s+tenant|a\s+roommate|roommates?)[^.]{0,100}"
        r"\b(?:take\s+over|fill|rent|sublet|share\s+my|move\s+into)\b"
        r")",
        re.IGNORECASE,
    )
    _WANTED_TERMS = re.compile(
        r"\b(?:looking|searching|seeking)\s+(?:for|to\s+(?:find|join|move))\b|"
        r"\bneed\s+(?:a|an)\s+(?:room|sublet|place|apartment|house)\b|"
        r"\blooking\s+to\s+(?:move|join\s+a\s+lease)\b",
        re.IGNORECASE,
    )

    def __init__(self, tokens: ApifyTokenStore):
        self.tokens = tokens
        self.search_url = "https://www.facebook.com/groups/"

    @staticmethod
    def _group_urls(preferences: Preferences) -> list[str]:
        configured = preferences.section("sources").get("facebook_group_urls", [])
        if not isinstance(configured, list):
            return []
        urls: list[str] = []
        for value in configured:
            parsed = urlsplit(str(value).strip())
            if parsed.scheme == "https" and parsed.netloc.casefold() in {"facebook.com", "www.facebook.com"}:
                if parsed.path.casefold().startswith("/groups/"):
                    urls.append(f"https://www.facebook.com{parsed.path.rstrip('/')}/")
        return list(dict.fromkeys(urls))

    @property
    def mode(self) -> str:
        return "automatic" if self.tokens.is_configured else "setup"

    @property
    def manual_reason(self) -> str:
        if not self.tokens.is_configured:
            return "Connect Apify before monitoring the configured public Facebook group."
        return ""

    @classmethod
    def _is_housing_offer(cls, text: str) -> bool:
        if not cls._HOUSING_TERMS.search(text):
            return False
        if not cls._WANTED_TERMS.search(text):
            # Ambiguous housing posts remain reviewable so terse real offers
            # are not lost. Wanted ads need explicit proof that a home exists.
            return True
        return bool(cls._STRONG_OFFER_TERMS.search(text))

    @staticmethod
    def _post_url(item: dict) -> str | None:
        value = str(item.get("url") or item.get("postUrl") or "")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {"facebook.com", "www.facebook.com"}:
            return None
        path = parsed.path.rstrip("/")
        if "/groups/" not in path.casefold() or not any(part in path.casefold() for part in ("/permalink/", "/posts/")):
            return None
        return f"https://www.facebook.com{path}/"

    @staticmethod
    def _title(text: str) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return _clean_text(first_line, 180) or "Facebook group housing post"

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            post_url = self._post_url(item)
            text = _clean_text(str(item.get("text") or item.get("caption") or ""), 1_400)
            if not post_url or not text or not self._is_housing_offer(text):
                continue
            if declared_outside_sf_area_hint(text):
                continue
            group_url = str(item.get("inputUrl") or item.get("facebookUrl") or "")
            group_title = _clean_text(str(item.get("groupTitle") or ""), 180)
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=hashlib.sha256(post_url.encode("utf-8")).hexdigest()[:20],
                    title=self._title(text),
                    original_url=post_url,
                    price=_parse_price(text, require_currency=True),
                    neighborhood=GmailHousingAlertSource._neighborhood(text, preferences),
                    listing_type="Group housing post",
                    summary=text,
                    metadata={
                        "listing_timestamp": item.get("time") or item.get("timestamp"),
                        "facebook_group_title": group_title,
                        "facebook_group_url": group_url,
                    },
                )
            )
        return listings

    def _search(
        self,
        client: httpx.Client,
        preferences: Preferences,
        *,
        results_limit: int,
    ) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        group_urls = self._group_urls(preferences)
        if not group_urls:
            return []
        self.search_url = group_urls[0]
        # The same shape as Marketplace: one deep read while the board is
        # empty, then only enough to catch what is new.
        first_scan = self.tokens.first_scan_pending("groups")
        if first_scan:
            results_limit = max(results_limit, self.first_scan_results_limit)
        if not self.tokens.reserve_monthly_group_posts(results_limit, self.monthly_post_limit):
            raise SourceError(
                f"The local {self.monthly_post_limit}-post monthly Facebook Groups safety cap was reached."
            )
        payload = {
            "startUrls": [{"url": url} for url in group_urls],
            "resultsLimit": results_limit,
            "viewOption": "CHRONOLOGICAL",
            # A month, because a room posted three weeks ago is still worth
            # seeing and a fortnight was throwing away half the board.
            "onlyPostsNewerThan": "30 days",
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError(
                f"The Facebook Groups helper exceeded its {int(self.request_timeout_seconds)}-second time limit."
            ) from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the token or the configured group is private. Only public groups can run here.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no additional group posts were imported.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Facebook Groups helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Facebook Groups helper returned an unexpected result format.")
        listings = self.parse_rows(rows, preferences)
        # Only once it has actually worked, so a first read that came back
        # empty keeps its deep pull rather than dropping to a trickle forever.
        if first_scan and listings:
            self.tokens.mark_first_scan_done("groups")
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return self._search(client, preferences, results_limit=self.results_limit)

    def search_for_trigger(
        self,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
    ) -> list[ListingCandidate]:
        results_limit = (
            self.manual_results_limit
            if trigger in {"manual", "facebook_groups_test"}
            else self.results_limit
        )
        return self._search(client, preferences, results_limit=results_limit)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class FurnishedFinderSource:
    """Legacy bounded Furnished Finder connector kept disabled by default.

    The actual supported path is the free Chrome bridge, which imports the
    results a normal browser has rendered. This class remains in the source
    list only so the dashboard can explain that setup before the first bridge
    import; it never spends Apify credit or runs unattended requests.
    """

    platform = "Furnished Finder"
    detail_budget = 0
    scheduled_only = True
    results_limit = 5
    monthly_listing_limit = 300
    request_timeout_seconds = 50.0
    actor_url = "https://api.apify.com/v2/acts/crawlerbros~furnished-finder-scraper/run-sync-get-dataset-items"
    search_url = "https://www.furnishedfinder.com/housing/us--ca--san-francisco"

    def __init__(self, tokens: ApifyTokenStore, *, enabled: bool = False):
        self.tokens = tokens
        # The current community actor was verified against the live account and
        # did not finish inside the product's two-minute scan budget. Leave it
        # opt-in until a provider proves it can do so reliably.
        self.enabled = enabled

    @property
    def mode(self) -> str:
        return "setup"

    @property
    def manual_reason(self) -> str:
        return "Install the free Furnished Finder Chrome bridge and sync one saved search from Alerts."

    @staticmethod
    def _property_url(value: object) -> str | None:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {
            "furnishedfinder.com",
            "www.furnishedfinder.com",
        }:
            return None
        if not parsed.path.casefold().startswith("/property/"):
            return None
        return f"https://www.furnishedfinder.com{parsed.path.rstrip('/')}"

    @staticmethod
    def _source_id(row: dict[str, object], url: str) -> str:
        for key in ("id", "listingId", "propertyId"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return _source_id(url)

    @staticmethod
    def _coordinate_neighborhood(row: dict[str, object]) -> str | None:
        try:
            latitude = float(row.get("latitude"))
            longitude = float(row.get("longitude"))
        except (TypeError, ValueError):
            return None
        return facebook_coordinate_neighborhood(f"{latitude} {longitude}")

    @staticmethod
    def _text_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_clean_text(str(item), 120) for item in value if _clean_text(str(item), 120)]

    @classmethod
    def _summary(cls, row: dict[str, object]) -> str:
        parts: list[str] = []
        for key in ("description", "spaceDescription", "neighborhoodDescription"):
            value = _clean_text(str(row.get(key) or ""), 520)
            if value:
                parts.append(value)
        if row.get("availableOnDate") or row.get("availableFromDate"):
            parts.append(f"Available: {row.get('availableOnDate') or row.get('availableFromDate')}")
        if row.get("minimumStayDays") not in (None, ""):
            parts.append(f"Minimum stay: {row['minimumStayDays']} days")
        if row.get("bathroomType"):
            parts.append(f"Bath: {row['bathroomType']}")
        amenities = cls._text_list(row.get("amenities"))
        if amenities:
            parts.append("Amenities: " + ", ".join(amenities[:12]))
        house_rules = cls._text_list(row.get("houseRules"))
        if house_rules:
            parts.append("House rules: " + ", ".join(house_rules[:8]))
        return _clean_text(". ".join(parts), 1_400)

    def parse_rows(self, rows: list[object], preferences: Preferences) -> list[ListingCandidate]:
        listings: list[ListingCandidate] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            row: dict[str, object] = item
            original_url = self._property_url(row.get("url") or row.get("listingUrl"))
            title = _clean_text(str(row.get("title") or row.get("propertyName") or ""), 220)
            if not original_url or not title:
                continue
            source_id = self._source_id(row, original_url)
            summary = self._summary(row)
            location_text = " ".join(
                str(row.get(key) or "") for key in ("address", "city", "state", "zip", "neighborhoodDescription")
            )
            neighborhood = self._coordinate_neighborhood(row) or GmailHousingAlertSource._neighborhood(
                location_text + " " + title + " " + summary,
                preferences,
            )
            property_type = _clean_text(
                str(row.get("propertyType") or row.get("propertyTypeClass") or "Furnished rental"),
                100,
            )
            price_value = row.get("monthlyPrice") or row.get("monthlyRent") or row.get("price")
            price = _parse_price(str(price_value), require_currency=False)
            if isinstance(price_value, (int, float)):
                price = int(price_value)
            metadata = {
                key: value
                for key, value in {
                    "available_from": row.get("availableOnDate") or row.get("availableFromDate"),
                    "minimum_stay_days": row.get("minimumStayDays"),
                    "furnished_finder_city": row.get("city"),
                    "furnished_finder_state": row.get("state"),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                }.items()
                if value not in (None, "")
            }
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=source_id,
                    title=title,
                    original_url=original_url,
                    price=price,
                    neighborhood=neighborhood,
                    # The actor's `room` filter is a structured private-room
                    # fact. Spell it out for the source-neutral scorer, rather
                    # than making an otherwise valid room look merely unknown.
                    listing_type=f"Private room · {property_type}",
                    summary=summary or title,
                    metadata={**metadata, "property_type": property_type},
                )
            )
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        token = self.tokens.token
        if not token:
            raise SourceError("The Apify token is missing or invalid; reconnect it from Alerts.")
        budget = preferences.section("budget")
        min_price = setting_int(budget.get("min_monthly"), 800)
        max_price = setting_int(budget.get("max_monthly"), 2700)
        source_settings = preferences.section("sources")
        try:
            results_limit = int(source_settings.get("furnished_finder_results_per_scan", self.results_limit))
            monthly_listing_limit = int(
                source_settings.get("furnished_finder_monthly_listing_limit", self.monthly_listing_limit)
            )
        except (TypeError, ValueError) as exc:
            raise SourceError("Furnished Finder limits in preferences must be whole numbers.") from exc
        # Keep scheduled work small even if someone hand-edits the advanced file.
        results_limit = max(1, min(results_limit, 10))
        monthly_listing_limit = max(results_limit, min(monthly_listing_limit, 300))
        if not self.tokens.reserve_monthly_furnished_finder_listings(
            results_limit,
            monthly_listing_limit,
        ):
            raise SourceError("The local 300-listing monthly Furnished Finder free-credit cap was reached.")
        payload = {
            "city": "San Francisco",
            "state": "CA",
            "maxItems": results_limit,
            "minPrice": min_price,
            "maxPrice": max_price,
            "propertyType": ["room"],
            "moreDetails": False,
            "includeReviews": False,
        }
        try:
            response = client.post(
                self.actor_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceError("The Furnished Finder helper exceeded its 50-second time limit.") from exc
        if response.status_code in {401, 403}:
            raise SourceError("Apify rejected the token or the Furnished Finder helper is unavailable.")
        if response.status_code in {402, 429}:
            raise SourceError("The Apify free-credit or rate limit was reached; no Furnished Finder listings were imported.")
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise SourceError("The Furnished Finder helper returned invalid JSON.") from exc
        if not isinstance(rows, list):
            raise SourceError("The Furnished Finder helper returned an unexpected result format.")
        return self.parse_rows(rows, preferences)

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class CraigslistSource:
    platform = "Craigslist"
    mode = "automatic"
    # Read on its own lane, alongside the rest of a scan rather than ahead of
    # it. The only source that does: it is served from Craigslist's own
    # addresses and shares no infrastructure with the portals behind
    # CloudFront, several of which already turn the app away, so reading it at
    # the same time as them changes nothing any site sees. It was 25 of the 29
    # seconds of a first search. Any other source opting in needs the same
    # argument made for it, and tests/test_scan_lanes.py fails if one does.
    runs_in_own_lane = True
    room_search_url = "https://sfbay.craigslist.org/search/sfc/roo"
    sublet_search_url = "https://sfbay.craigslist.org/search/sfc/sub"
    unit_search_url = "https://sfbay.craigslist.org/search/sfc/apa"
    two_bedroom_search_url = unit_search_url
    three_bedroom_search_url = unit_search_url
    four_bedroom_search_url = unit_search_url
    base_search_url = room_search_url
    manual_reason = None

    def __init__(self) -> None:
        self.search_url = f"{self.base_search_url}?sort=date"
        self.detail_budget = 10

    def _room_url(self, preferences: Preferences) -> str:
        query: dict[str, str | int] = {"sort": "date"}
        budget = preferences.section("budget")
        if budget.get("min_monthly") is not None:
            query["min_price"] = int(budget["min_monthly"])
        if budget.get("max_monthly") is not None:
            query["max_price"] = int(budget["max_monthly"])
        return f"{self.room_search_url}?{urlencode(query)}"

    def _unit_url(self, preferences: Preferences) -> str:
        settings = preferences.section("whole_unit")
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": setting_int(settings.get("max_monthly"), 3000),
            "min_bedrooms": 0,
            "max_bedrooms": 1,
        }
        return f"{self.unit_search_url}?{urlencode(query)}"

    def _sublet_url(self, preferences: Preferences) -> str:
        """Search Craigslist's dedicated sublet category, then classify by home shape."""
        enabled = set(preferences.deal_profile.enabled_paths)
        ceilings: list[int] = []
        if "private_room" in enabled:
            ceilings.append(setting_int(preferences.section("budget").get("max_monthly"), 2700))
        whole_unit = preferences.section("whole_unit")
        two_bedroom = preferences.section("two_bedroom")
        three_bedroom = preferences.section("three_bedroom")
        if enabled.intersection({"studio", "one_bedroom"}):
            ceilings.append(setting_int(whole_unit.get("max_monthly"), 3000))
        if "two_bedroom" in enabled:
            ceilings.append(setting_int(two_bedroom.get("occupants"), 2) * setting_int(two_bedroom.get("max_per_person"), 2700))
        if "three_bedroom" in enabled:
            ceilings.append(setting_int(three_bedroom.get("occupants"), 3) * setting_int(three_bedroom.get("max_per_person"), 2500))
        maximum = max(ceilings or [3000])
        query: dict[str, str | int] = {"sort": "date", "max_price": maximum}
        budget = preferences.section("budget")
        if budget.get("min_monthly") is not None:
            query["min_price"] = int(budget["min_monthly"])
        return f"{self.sublet_search_url}?{urlencode(query)}"

    def _two_bedroom_url(self, preferences: Preferences) -> str:
        """Build an exact 2-bedroom search with its own hard cap."""
        two_bedroom = preferences.section("two_bedroom")
        two_bedroom_max = setting_int(two_bedroom.get("occupants"), 2) * int(
            two_bedroom.get("max_per_person", 2700)
        )
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": two_bedroom_max,
            "min_bedrooms": 2,
            "max_bedrooms": 2,
        }
        return f"{self.two_bedroom_search_url}?{urlencode(query)}"

    def _four_bedroom_url(self, preferences: Preferences) -> str:
        """Exact 4-bedroom inventory, which 2-3 bedroom volume would otherwise bury."""
        settings = preferences.section("four_bedroom")
        max_monthly = setting_int(settings.get("occupants"), 4) * setting_int(settings.get("max_per_person"), 2300)
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": max_monthly,
            "min_bedrooms": 4,
            "max_bedrooms": 4,
        }
        return f"{self.four_bedroom_search_url}?{urlencode(query)}"

    def _three_bedroom_url(self, preferences: Preferences) -> str:
        """Return exact 3-bedroom inventory so 2-bedroom volume cannot hide it."""
        settings = preferences.section("three_bedroom")
        max_monthly = setting_int(settings.get("occupants"), 3) * int(
            settings.get("max_per_person", 2500)
        )
        query: dict[str, str | int] = {
            "sort": "date",
            "max_price": max_monthly,
            "min_bedrooms": 3,
            "max_bedrooms": 3,
        }
        return f"{self.three_bedroom_search_url}?{urlencode(query)}"

    @staticmethod
    def _parse_search(
        response: httpx.Response,
        *,
        max_results: int,
        search_kind: str,
    ) -> list[ListingCandidate]:
        soup = BeautifulSoup(response.text, "html.parser")
        result_nodes = soup.select("li.cl-static-search-result")
        if not result_nodes:
            raise SourceError("Craigslist returned no recognizable result cards; its page format may have changed.")

        listings: list[ListingCandidate] = []
        for node in result_nodes[:max_results]:
            anchor = node.select_one("a[href]")
            title_node = node.select_one(".title")
            if not anchor or not title_node:
                continue
            original_url = urljoin(str(response.url), anchor.get("href", ""))
            title = _clean_text(title_node.get_text(" ", strip=True))
            location_node = node.select_one(".location")
            price_node = node.select_one(".price")
            if not original_url or not title:
                continue
            area_label = _clean_text(location_node.get_text(" ", strip=True)) if location_node else None
            # A San Francisco search is padded with the rest of the Bay Area
            # when it runs thin. Those homes are not near matches for an SF
            # search, and collecting them spent the detail budget and filled
            # the archive with places nobody asked about.
            if declared_outside_sf_url_hint(original_url) or outside_sf_location_label(area_label):
                continue
            listings.append(
                ListingCandidate(
                    platform="Craigslist",
                    source_id=_source_id(original_url),
                    title=title,
                    original_url=original_url,
                    price=_parse_price(price_node.get_text(" ", strip=True) if price_node else None),
                    neighborhood=area_label,
                    listing_type=(
                        "Room/share"
                        if search_kind == ROOM
                        else "Sublet / temporary rental"
                        if search_kind == "sublet"
                        else "Apartment rental"
                    ),
                    summary=title,
                    metadata={"craigslist_search_kind": search_kind},
                    housing_kind=ROOM if search_kind == ROOM else UNKNOWN if search_kind == "sublet" else WHOLE_UNIT,
                )
            )
        if not listings:
            raise SourceError("Craigslist result cards were present but none could be parsed.")
        return listings

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        source_settings = preferences.section("sources")
        enabled = set(preferences.deal_profile.enabled_paths)
        room_enabled = "private_room" in enabled
        unit_enabled = bool(enabled.intersection({"studio", "one_bedroom"}))
        two_enabled = "two_bedroom" in enabled
        three_enabled = "three_bedroom" in enabled
        four_enabled = "four_bedroom" in enabled
        room_details = setting_int(source_settings.get("craigslist_detail_pages_per_scan"), 10)
        unit_details = setting_int(source_settings.get("craigslist_unit_detail_pages_per_scan"), 10)
        two_bedroom_details = setting_int(source_settings.get("craigslist_two_bedroom_detail_pages_per_scan"), 10)
        three_bedroom_details = int(
            source_settings.get("craigslist_three_bedroom_detail_pages_per_scan", 25)
        )
        four_bedroom_details = int(
            source_settings.get("craigslist_four_bedroom_detail_pages_per_scan", 15)
        )
        sublet_details = setting_int(source_settings.get("craigslist_sublet_detail_pages_per_scan"), 15)
        room_details = max(0, min(room_details, 50))
        unit_details = max(0, min(unit_details, 50))
        two_bedroom_details = max(0, min(two_bedroom_details, 50))
        three_bedroom_details = max(0, min(three_bedroom_details, 50))
        four_bedroom_details = max(0, min(four_bedroom_details, 50))
        sublet_details = max(0, min(sublet_details, 50))
        room_details = room_details if room_enabled else 0
        unit_details = unit_details if unit_enabled else 0
        two_bedroom_details = two_bedroom_details if two_enabled else 0
        three_bedroom_details = three_bedroom_details if three_enabled else 0
        four_bedroom_details = four_bedroom_details if four_enabled else 0
        self.detail_budget = (
            room_details
            + unit_details
            + two_bedroom_details
            + three_bedroom_details
            + four_bedroom_details
            + sublet_details
        )
        max_results = setting_int(source_settings.get("max_results_per_source"), 250)
        room_url = self._room_url(preferences)
        unit_url = self._unit_url(preferences)
        two_bedroom_url = self._two_bedroom_url(preferences)
        three_bedroom_url = self._three_bedroom_url(preferences)
        four_bedroom_url = self._four_bedroom_url(preferences)
        sublet_url = self._sublet_url(preferences)
        search_requests = []
        if four_enabled:
            search_requests.append(("four_bedroom", four_bedroom_url))
        if three_enabled:
            search_requests.append(("three_bedroom", three_bedroom_url))
        if two_enabled:
            search_requests.append(("two_bedroom", two_bedroom_url))
        if unit_enabled:
            search_requests.append(("whole_unit", unit_url))
        # The dedicated sublet feed can contain any enabled home shape.
        search_requests.append(("sublet", sublet_url))
        if room_enabled:
            search_requests.append(("room", room_url))
        self.search_url = search_requests[0][1]
        parsed: dict[str, list[ListingCandidate]] = {
            "four_bedroom": [],
            "three_bedroom": [],
            "two_bedroom": [],
            "whole_unit": [],
            "sublet": [],
            "room": [],
        }
        errors: list[str] = []
        for search_kind, url in search_requests:
            try:
                response = client.get(url)
                response.raise_for_status()
                parsed[search_kind] = self._parse_search(
                    response,
                    max_results=max_results,
                    search_kind=search_kind,
                )
            except Exception as exc:
                errors.append(f"{search_kind}: {exc}")
        three_bedroom_listings = parsed["three_bedroom"]
        two_bedroom_listings = parsed["two_bedroom"]
        unit_listings = parsed["whole_unit"]
        sublet_listings = parsed["sublet"]
        room_listings = parsed["room"]
        if not three_bedroom_listings and not two_bedroom_listings and not unit_listings and not sublet_listings and not room_listings:
            raise SourceError("All Craigslist searches failed: " + "; ".join(errors))
        target_phrases = [
            phrase
            for area in (
                preferences.list_value("ideal_neighborhoods")
                + preferences.list_value("preferred_neighborhoods")
                + preferences.list_value("acceptable_neighborhoods")
            )
            for phrase in (area, *preferences.mapping_value("neighborhood_aliases").get(area, []))
            if _normal_text(phrase)
        ]

        def target_area_first(listing: ListingCandidate) -> int:
            location = _normal_text(listing.neighborhood or "")
            return 0 if any(
                re.search(rf"(?<!\w){re.escape(_normal_text(phrase))}(?!\w)", location)
                for phrase in target_phrases
            ) else 1

        # Detail requests are the scarce part of a scan. Preserve Craigslist's
        # newest-first order within each group, but enrich target-area cards
        # before generic or out-of-area cards.
        three_bedroom_listings.sort(key=target_area_first)
        two_bedroom_listings.sort(key=target_area_first)
        unit_listings.sort(key=target_area_first)
        sublet_listings.sort(key=target_area_first)
        room_listings.sort(key=target_area_first)
        def interleave(groups: tuple[list[ListingCandidate], ...]) -> list[ListingCandidate]:
            combined: list[ListingCandidate] = []
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        combined.append(group[index])
            return combined

        # Put each mode's configured detail quota first. This makes the knobs
        # real rather than merely summing them while a three-way interleave
        # accidentally allocates equal shares. Remaining cards are still
        # imported afterward with their thinner search-card facts.
        selected_groups = (
            three_bedroom_listings[:three_bedroom_details],
            two_bedroom_listings[:two_bedroom_details],
            unit_listings[:unit_details],
            sublet_listings[:sublet_details],
            room_listings[:room_details],
        )
        remaining_groups = (
            three_bedroom_listings[three_bedroom_details:],
            two_bedroom_listings[two_bedroom_details:],
            unit_listings[unit_details:],
            sublet_listings[sublet_details:],
            room_listings[room_details:],
        )
        listings = interleave(selected_groups) + interleave(remaining_groups)
        deduplicated: dict[str, ListingCandidate] = {}
        for listing in listings:
            deduplicated.setdefault(listing.original_url, listing)
        return list(deduplicated.values())

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        response = client.get(listing.original_url)
        # Craigslist answers a deleted post with 410, and a post that never
        # existed with 404. Both are the source stating plainly that the home is
        # not there, so they are an answer rather than a failure -- raising here
        # meant a deleted post looked exactly like a dropped connection, and the
        # home kept its place on the shortlist.
        if response.status_code in (404, 410):
            return replace(
                listing,
                metadata={
                    **listing.metadata,
                    "craigslist_detail_checked": True,
                    "verified_inactive": True,
                    "verification_concern": (
                        f"Verified inactive: Craigslist answered {response.status_code} for this post."
                    ),
                },
            )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        metadata = dict(listing.metadata)
        if soup.select_one(".removed") or _CRAIGSLIST_REMOVED_PATTERN.search(response.text):
            metadata.update(
                {
                    "craigslist_detail_checked": True,
                    "verified_inactive": True,
                    "verification_concern": "Verified inactive: Craigslist has removed or flagged this post.",
                }
            )
            return replace(listing, metadata=metadata, original_url=str(response.url))
        posted = soup.select_one("time.date[datetime], time[datetime]")
        if posted is not None:
            stamp = _clean_text(posted.get("datetime"), 40)
            if stamp:
                metadata["listing_timestamp"] = stamp
        description_meta = soup.select_one('meta[name="description"]')
        body = soup.select_one("#postingbody")
        body_text = _clean_text(body.get_text(" ", strip=True) if body else "")
        summary = _clean_text(
            body_text or description_meta.get("content", "") if description_meta else body_text,
            420,
        )
        attributes = [_clean_text(node.get_text(" ", strip=True)) for node in soup.select(".attrgroup .attr")]
        housing = soup.select_one(".postingtitle .housing")
        metadata["craigslist_detail_checked"] = True
        if attributes:
            metadata["attributes"] = " | ".join(attributes)
        attributes_text = " ".join(attributes)

        # Do not promote detail pages whose source facts contradict one another
        # or whose content proves it belongs to another metro.  This stays
        # intentionally narrow: a clean but sparse post remains reviewable;
        # only concrete conflicts are rejected.
        published_bedrooms = _craigslist_bedroom_count(housing.get_text(" ", strip=True) if housing else None)
        described_bedrooms = _craigslist_bedroom_count(" ".join(filter(None, [listing.title, body_text])))
        if (
            published_bedrooms is not None
            and described_bedrooms is not None
            and published_bedrooms != described_bedrooms
        ):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: the Craigslist bedroom count conflicts with the title or detail text."
                    ),
                }
            )
        elif _CRAIGSLIST_OFF_MARKET_LOCATION_PATTERN.search(body_text):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: the detail text references a location or transit system outside San Francisco."
                    ),
                }
            )
        elif _CRAIGSLIST_SUSPECT_PROFILE_PATTERN.search(attributes_text):
            metadata.update(
                {
                    "craigslist_content_rejected": True,
                    "verification_concern": (
                        "Rejected: recurring malformed lister-profile pattern seen in invalid Craigslist posts."
                    ),
                }
            )
        outside_area = declared_outside_sf_area_hint(body_text)
        declared_area = declared_sf_area_hint(body_text)
        resolved_area = outside_area or declared_area
        if resolved_area:
            metadata["detail_declared_neighborhood"] = resolved_area
        return replace(
            listing,
            summary=summary or listing.summary,
            neighborhood=resolved_area or listing.neighborhood,
            listing_type=_clean_text(housing.get_text(" ", strip=True)) if housing else listing.listing_type,
            metadata=metadata,
            original_url=str(response.url),
        )


class SpareRoomSource:
    # Answers only for itself, so it is read beside the rest rather than
    # behind them. See traffic_group() for what "itself" means here.
    runs_in_own_lane = True
    platform = "SpareRoom"
    # No claim. One request reads the first result page, and the URL asks for
    # no page after it; 12 successful runs on a real board each returned
    # exactly 11 rooms. A room that drops off that page has been pushed down
    # it, not taken down.
    search_covers_inventory = False
    mode = "automatic"
    search_url = "https://www.spareroom.com/rooms-for-rent/san_francisco?sort_by=last_updated"
    manual_reason = None
    detail_budget = 0

    def not_needed_for(self, preferences: Preferences) -> str | None:
        """Why this deal has no use for SpareRoom, or None when it has.

        Asked by the scanner before anything is read. It used to be answered
        from inside ``search``, as an empty list, and every scan then recorded
        a successful check of SpareRoom that had found nothing: the page said
        "working, no matches -- a valid result, not a failure" about a site no
        request had gone to, for a deal with no private room in it.
        """
        if "private_room" in preferences.deal_profile.enabled_paths:
            return None
        return "Private rooms are not in Your deal, and SpareRoom lists nothing else, so it was not asked."

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        max_results = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
        response = client.get(self.search_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        result_nodes = soup.select("li.listing-result")
        if not result_nodes:
            raise SourceError("SpareRoom returned no recognizable result cards; its page format may have changed.")
        listings: list[ListingCandidate] = []
        for node in result_nodes[:max_results]:
            anchor = node.select_one("a.listing-card__link[href]")
            if not anchor:
                continue
            original_url = urljoin(str(response.url), anchor.get("href", ""))
            listing_id = node.get("data-listing-id") or _source_id(original_url)
            title_node = node.select_one(".listing-card__title")
            title = _clean_text(
                title_node.get_text(" ", strip=True) if title_node else node.get("data-listing-title", "")
            )
            if not title or not original_url:
                continue
            description = node.select_one(".listing-card__short_description")
            room_type = node.select_one(".listing-card__room-type")
            metadata = {
                key: value
                for key, value in {
                    "property_type": node.get("data-listing-property-type"),
                    "rooms_in_property": node.get("data-listing-rooms-in-property"),
                    "available_now": node.get("data-listing-available-now"),
                    "advertiser_role": node.get("data-listing-advertiser-role"),
                }.items()
                if value not in (None, "")
            }
            listings.append(
                ListingCandidate(
                    platform=self.platform,
                    source_id=str(listing_id),
                    title=title,
                    original_url=original_url,
                    price=_parse_price(str(node.get("data-listing-ad-rate-normalised", ""))),
                    neighborhood=_clean_text(str(node.get("data-listing-neighbourhood", ""))) or None,
                    listing_type=_clean_text(room_type.get_text(" ", strip=True)) if room_type else "Room/share",
                    summary=_clean_text(description.get_text(" ", strip=True), 420) if description else title,
                    metadata=metadata,
                )
            )
        if not listings:
            raise SourceError("SpareRoom result cards were present but none could be parsed.")
        return listings

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


class ManualSource:
    mode = "manual"
    detail_budget = 0

    def __init__(self, platform: str, search_url: str, reason: str):
        self.platform = platform
        self.search_url = search_url
        self.manual_reason = reason

    def search(self, client: httpx.Client, preferences: Preferences) -> list[ListingCandidate]:
        return []

    def enrich(self, client: httpx.Client, listing: ListingCandidate) -> ListingCandidate:
        return listing


def default_sources(
    mailbox: GmailAlertMailbox | None = None,
    apify_tokens: ApifyTokenStore | None = None,
) -> list[ListingSource]:
    blocked_reason = "Public pages reject unattended requests (HTTP 403); use the direct search link."
    # Named rather than indexed: the dashboard's ordering below interleaves these
    # with the setup-required sources, and positional slicing made adding a
    # source silently reorder the page.
    craigslist = CraigslistSource()
    listings_project = ListingsProjectSource()
    abacus = AbacusSource()
    spareroom = SpareRoomSource()
    sf_portal = SFHousingPortalSource()
    zumper = ZumperSource()
    apartment_list = ApartmentListSource()
    uloop = UloopSource()
    udr = UDRSource()
    appfolio = AppFolioSource()
    avalonbay = AvalonBaySource()
    rentsfnow = RentSFNowSource()
    redfin = RedfinSource()
    rent_com = RentComSource()
    apartment_guide = ApartmentGuideSource()
    movoto = MovotoSource()
    trulia = TruliaSource()
    zillow = ZillowSource()
    free_sources: list[ListingSource] = [craigslist, listings_project, abacus]
    manual_sources: list[ListingSource] = [
        ManualSource("HotPads", "https://hotpads.com/san-francisco-ca/apartments-for-rent", blocked_reason),
        ManualSource("Apartments.com", "https://www.apartments.com/san-francisco-ca/", blocked_reason),
        ManualSource("Roomies", "https://www.roomies.com/rooms/san-francisco-ca", blocked_reason),
    ]
    if mailbox:
        # Preserve the dashboard's requested source ordering.
        facebook: ListingSource = (
            FacebookMarketplaceSource(mailbox, apify_tokens)
            if apify_tokens is not None
            else FacebookMarketplaceAlertSource(mailbox)
        )
        return [
            facebook,
            FacebookGroupsSource(apify_tokens) if apify_tokens is not None else ManualSource(
                "Facebook Groups",
                "https://www.facebook.com/groups/",
                "Connect Apify before monitoring a configured public Facebook group.",
            ),
            *free_sources,
            FurnishedFinderSource(apify_tokens) if apify_tokens is not None else ManualSource(
                "Furnished Finder",
                FurnishedFinderSource.search_url,
                "Connect Apify before checking Furnished Finder's blocked public search.",
            ),
            HotPadsAlertSource(mailbox),
            ApartmentsComAlertSource(mailbox),
            spareroom,
            sf_portal,
            apartment_list,
            zumper,
            uloop,
            udr,
            appfolio,
            avalonbay,
            rentsfnow,
            redfin,
            rent_com,
            apartment_guide,
            movoto,
            trulia,
            zillow,
            RoomiesAlertSource(mailbox),
        ]
    return [
        ManualSource(
            "Facebook Marketplace",
            "https://www.facebook.com/marketplace/you/alerts/",
            "Marketplace saved-search alert email setup is not connected yet.",
        ),
        ManualSource(
            "Facebook Groups",
            "https://www.facebook.com/groups/",
            "Connect Apify before monitoring a configured public Facebook group.",
        ),
        *free_sources,
        ManualSource(
            "Furnished Finder",
            FurnishedFinderSource.search_url,
            "Connect Apify before checking Furnished Finder's blocked public search.",
        ),
        *manual_sources[:1],
        spareroom,
        sf_portal,
        apartment_list,
        zumper,
        uloop,
        udr,
        appfolio,
        avalonbay,
        rentsfnow,
        redfin,
        rent_com,
        apartment_guide,
        movoto,
        trulia,
        zillow,
        *manual_sources[1:],
    ]
