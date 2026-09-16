"""Who sees our requests, and what may therefore be read at the same time.

A scan reads on two lines at once. That is only safe while no site sees two
requests from us at the same moment, and a site is not a hostname: one company
answers for several of them. Zillow Group fronts Zillow, Trulia and HotPads;
CoStar fronts Rent.com, ApartmentGuide and Apartments.com. On the evening this
was written all of those refused us inside a minute -- 503 from Zillow, 403
from Trulia, 429 from Rent.com and ApartmentGuide -- while every independent
site answered normally, which is what a shared rate limiter looks like.

So each source declares whose limiter it answers to, and the rule is simple
enough to test: a group is read on one line or the other, never both.
"""

from sf_housing.scanner import reads_on_its_own_lane
from sf_housing.sources import default_sources, traffic_group


def groups() -> dict[str, str]:
    return {source.platform: traffic_group(source) for source in default_sources()}


def test_one_company_behind_several_sites_is_one_group() -> None:
    """The regression this exists to prevent: reading Zillow and Trulia at the
    same moment because they have different hostnames, when one company counts
    both against us."""
    by_platform = groups()
    assert by_platform["Zillow"] == by_platform["Trulia"] == by_platform["HotPads"]
    assert by_platform["Rent.com"] == by_platform["ApartmentGuide"] == by_platform["Apartments.com"]
    assert by_platform["Zillow"] != by_platform["Rent.com"], "two companies, two groups"


def test_two_sites_on_one_domain_are_one_group() -> None:
    """AppFolio serves two property managers here, and Facebook two sources.
    The domain answers for both, so neither pair may be split across lines."""
    by_platform = groups()
    assert by_platform["AppFolio"] == by_platform["Abacus (small buildings)"]
    assert by_platform["Facebook Marketplace"] == by_platform["Facebook Groups"]


def test_independent_sites_are_their_own_groups() -> None:
    by_platform = groups()
    independent = ["Craigslist", "Zumper", "Uloop", "AvalonBay", "SF Housing Portal", "Movoto"]
    assert len({by_platform[name] for name in independent}) == len(independent)


def test_no_company_is_read_on_both_lines_at_once() -> None:
    """The load-bearing rule. Everything else about the lane is bookkeeping;
    this is the one that keeps a site from seeing two requests at once."""
    lane, main = set(), set()
    for source in default_sources():
        (lane if reads_on_its_own_lane(source) else main).add(traffic_group(source))

    both = sorted(lane & main)
    assert both == [], f"read on both lines at once: {both}"


def test_the_lane_holds_only_sites_that_answer_for_themselves() -> None:
    """Named rather than inferred. A new source joins the lane only when
    somebody has decided its owner sees nobody else's requests."""
    laned = {source.platform for source in default_sources() if reads_on_its_own_lane(source)}
    for family in ("Zillow", "Trulia", "HotPads", "Rent.com", "ApartmentGuide", "Apartments.com"):
        assert family not in laned, f"{family} answers to a limiter that also sees other sources"
    assert "Craigslist" in laned
