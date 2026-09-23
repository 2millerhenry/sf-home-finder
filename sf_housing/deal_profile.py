from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping


PROFILE_VERSION = 1
PROFILE_STATES = {"draft", "active"}
HOUSING_PATHS = (
    "private_room",
    "studio",
    "one_bedroom",
    "two_bedroom",
    "three_bedroom",
    "four_bedroom",
)
# Paths where the rent is split, so the budget is stated per person.
SPLIT_PATHS = ("two_bedroom", "three_bedroom", "four_bedroom")
DEFAULT_OCCUPANTS = {"two_bedroom": 2, "three_bedroom": 3, "four_bedroom": 4}
DEFAULT_PER_PERSON = {"two_bedroom": 2700, "three_bedroom": 2500, "four_bedroom": 2300}
BEDROOM_PATHS = {2: "two_bedroom", 3: "three_bedroom", 4: "four_bedroom"}

AREA_TIERS = ("dream", "strong", "okay", "avoid")
# What the form calls each group. Stored keys and shown names have to agree
# wherever a message names a group, or the page asks you to fix a column that
# is not on it.
AREA_TIER_LABELS = {"dream": "Dream", "strong": "Strong", "okay": "Secondary", "avoid": "Avoid"}
IMPORTANCE_LEVELS = {"must_have", "important", "nice", "ignore", "avoid"}

# Names shown in the normal form. Sources still normalize aliases separately,
# but a new user should be able to choose any recognizable SF neighborhood
# without writing their own text or learning source-specific labels.
SF_NEIGHBORHOODS = (
    "Alamo Square",
    "Anza Vista",
    "Balboa Park",
    "Bayview",
    "Bernal Heights",
    "Castro",
    "Chinatown",
    "Civic Center",
    "Cole Valley",
    "Cow Hollow",
    "Crocker Amazon",
    "Diamond Heights",
    "Dogpatch",
    "Duboce Triangle",
    "Embarcadero",
    "Excelsior",
    "Financial District",
    "Forest Hill",
    "Eureka Valley",
    "Glen Park",
    "Haight-Ashbury",
    "Hayes Valley",
    "Ingleside",
    "Inner Parkside",
    "Inner Richmond",
    "Inner Sunset",
    "Lower Haight",
    "Mission Bay",
    "Marina",
    "Mission District",
    "Mission Dolores",
    "Nob Hill",
    "Noe Valley",
    "NOPA",
    "North Beach",
    "Oceanview",
    "Outer Mission",
    "Outer Richmond",
    "Outer Sunset",
    "Pacific Heights",
    "Park Merced",
    "Parkside",
    "Potrero Hill",
    "Portola",
    "Presidio Heights",
    "Russian Hill",
    "Sea Cliff",
    "SoMa",
    "St. Francis Wood",
    "Sunnyside",
    "Telegraph Hill",
    "Tenderloin",
    "Twin Peaks",
    "Visitacion Valley",
    "West Portal",
    "Western Addition",
    "Richmond District",
)


class DealProfileError(ValueError):
    pass


def _clean_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _join_plainly(items: list[str]) -> str:
    """A, B and C -- the way somebody reads a list aloud."""
    items = [item for item in items if item]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _plain_date(value: str) -> str:
    """1 October rather than 2026-10-01, and the raw value if it will not parse.

    The review sentence is read, not queried, and a half-typed date arrives here
    from every keystroke in a date field.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    # The year only when it is not the year the reader is already in: "1 October
    # 2026" in 2026 is three words to say "soon".
    day = f"{parsed.day} {parsed.strftime('%B')}"
    return day if parsed.year == date.today().year else f"{day} {parsed.year}"


def _plain_lease(minimum: int | None, maximum: int | None) -> str:
    """What the lease answers mean, or nothing at all when neither was set."""

    def months(count: int) -> str:
        return f"{count} month" if count == 1 else f"{count} months"

    if minimum and maximum:
        if minimum == maximum:
            return f"Leases of exactly {months(minimum)}."
        return f"Leases of {minimum} to {months(maximum)}."
    if minimum:
        return f"Leases of at least {months(minimum)}."
    if maximum:
        return f"Leases of at most {months(maximum)}."
    return ""


def _shared_unit_ceiling(profile: DealProfile) -> int | None:
    """The one building-size answer the form shows for every whole home."""
    limits = [
        budget.maximum_building_units
        for path, budget in profile.budgets.items()
        if path != "private_room"
    ]
    if not limits:
        return 50
    return None if any(limit is None for limit in limits) else max(limits)


def _unit_ceiling(value: object) -> int | None:
    """A building-size ceiling, where "no limit" is one of the real answers.

    Stored as None rather than as a number large enough to never match, so the
    difference between "I want a small building" and "I do not mind" survives
    a save, and so nothing has to print a ceiling nobody chose.
    """
    if value in (None, "", "any", "Any"):
        return None
    return _positive_int(value, "maximum building units")


def _positive_int(value: object, field_name: str, *, required: bool = True) -> int | None:
    if value in (None, "") and not required:
        return None
    if isinstance(value, bool):
        raise DealProfileError(f"{field_name} must be a positive whole number.")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise DealProfileError(f"{field_name} must be a positive whole number.") from exc
    if parsed <= 0:
        raise DealProfileError(f"{field_name} must be a positive whole number.")
    return parsed


@dataclass(frozen=True, slots=True)
class PathBudget:
    maximum_monthly: int
    ideal_monthly: int | None = None
    minimum_monthly: int | None = None
    preferred_minimum: int | None = None
    preferred_maximum: int | None = None
    occupants: int = 1
    maximum_building_units: int | None = None

    @property
    def total_maximum(self) -> int:
        return self.maximum_monthly * self.occupants if self.occupants > 1 else self.maximum_monthly

    def validate(self, path: str) -> None:
        if self.maximum_monthly <= 0:
            raise DealProfileError(f"Set a maximum monthly budget for {path.replace('_', ' ')}.")
        if self.ideal_monthly is not None and not 0 < self.ideal_monthly <= self.maximum_monthly:
            raise DealProfileError(
                f"The ideal {path.replace('_', ' ')} price cannot exceed its maximum."
            )
        if self.minimum_monthly is not None and not 0 < self.minimum_monthly <= self.maximum_monthly:
            raise DealProfileError(
                f"The minimum {path.replace('_', ' ')} price must be below its maximum."
            )
        if self.occupants <= 0:
            raise DealProfileError(f"The {path.replace('_', ' ')} occupant count must be positive.")
        if self.maximum_building_units is not None and self.maximum_building_units <= 0:
            raise DealProfileError("The building-size maximum must be positive.")

    def to_dict(self) -> dict[str, int]:
        result: dict[str, int] = {"maximum_monthly": self.maximum_monthly}
        for name in (
            "ideal_monthly",
            "minimum_monthly",
            "preferred_minimum",
            "preferred_maximum",
            "maximum_building_units",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.occupants != 1:
            result["occupants"] = self.occupants
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object], path: str) -> "PathBudget":
        maximum = _positive_int(data.get("maximum_monthly"), f"{path} maximum")
        budget = cls(
            maximum_monthly=int(maximum),
            ideal_monthly=_positive_int(data.get("ideal_monthly"), f"{path} ideal", required=False),
            minimum_monthly=_positive_int(data.get("minimum_monthly"), f"{path} minimum", required=False),
            preferred_minimum=_positive_int(data.get("preferred_minimum"), f"{path} preferred minimum", required=False),
            preferred_maximum=_positive_int(data.get("preferred_maximum"), f"{path} preferred maximum", required=False),
            occupants=int(_positive_int(data.get("occupants", 1), f"{path} occupants")),
            maximum_building_units=_positive_int(
                data.get("maximum_building_units"), "maximum building units", required=False
            ),
        )
        budget.validate(path)
        return budget


@dataclass(frozen=True, slots=True)
class DealProfile:
    version: int = PROFILE_VERSION
    state: str = "draft"
    enabled_paths: tuple[str, ...] = ()
    budgets: dict[str, PathBudget] = field(default_factory=dict)
    anywhere_in_sf: bool = False
    areas: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {tier: () for tier in AREA_TIERS}
    )
    move_in_flexible: bool = True
    earliest_move_in: str | None = None
    preferred_by: str | None = None
    lease_min_months: int | None = None
    lease_max_months: int | None = None
    private_room_required: bool = True
    household_maximum: int | None = None
    preferences: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state == "active"

    def validate(self, *, require_active: bool | None = None) -> None:
        if self.version != PROFILE_VERSION:
            raise DealProfileError(
                f"Unsupported deal profile version {self.version}; expected {PROFILE_VERSION}."
            )
        if self.state not in PROFILE_STATES:
            raise DealProfileError("Deal profile state must be draft or active.")
        unknown_paths = set(self.enabled_paths) - set(HOUSING_PATHS)
        if unknown_paths:
            raise DealProfileError("Unknown housing path: " + ", ".join(sorted(unknown_paths)))
        should_be_complete = self.active if require_active is None else require_active
        if should_be_complete and not self.enabled_paths:
            raise DealProfileError("Choose at least one kind of home.")
        for path in self.enabled_paths:
            budget = self.budgets.get(path)
            if budget is None and should_be_complete:
                raise DealProfileError(f"Set a budget for {path.replace('_', ' ')}.")
            if budget is not None:
                budget.validate(path)
        normalized_areas: dict[str, str] = {}
        for tier in AREA_TIERS:
            for area in self.areas.get(tier, ()):
                key = area.casefold()
                previous = normalized_areas.get(key)
                if previous:
                    raise DealProfileError(
                        f"{area} appears in both {AREA_TIER_LABELS[previous]} and "
                        f"{AREA_TIER_LABELS[tier]}. Choose one area priority."
                    )
                normalized_areas[key] = tier
        if should_be_complete and not self.anywhere_in_sf and not any(
            self.areas.get(tier) for tier in ("dream", "strong", "okay")
        ):
            raise DealProfileError("Choose at least one acceptable SF neighborhood or Anywhere in SF.")
        parsed_dates: list[tuple[str, date]] = []
        for name, value in (
            ("earliest move-in", self.earliest_move_in),
            ("preferred move-in", self.preferred_by),
        ):
            if value:
                try:
                    parsed_dates.append((name, date.fromisoformat(value)))
                except ValueError as exc:
                    raise DealProfileError(f"Use a valid date for {name}.") from exc
        if len(parsed_dates) == 2 and parsed_dates[1][1] < parsed_dates[0][1]:
            raise DealProfileError("The preferred move-in date cannot be before the earliest date.")
        if (
            self.lease_min_months is not None
            and self.lease_max_months is not None
            and self.lease_min_months > self.lease_max_months
        ):
            raise DealProfileError("The minimum lease cannot be longer than the maximum lease.")
        unknown_importance = set(self.preferences.values()) - IMPORTANCE_LEVELS
        if unknown_importance:
            raise DealProfileError("Unknown preference importance: " + ", ".join(sorted(unknown_importance)))

    def summary(self) -> str:
        """The whole deal in plain English, as the form holds it right now.

        Every answer that changes what reaches the shortlist belongs here. The
        review section is read as the last check before saving, so anything it
        leaves out is an answer somebody has to scroll back up to find -- and a
        size ticked with the price still blank used to be invisible in it
        entirely, which made the save afterwards refuse for a reason the review
        had never mentioned.
        """
        labels = {
            "private_room": "private rooms",
            "studio": "studios",
            "one_bedroom": "1-bedrooms",
            "two_bedroom": "2-bedroom splits",
            "three_bedroom": "3-bedroom splits",
            "four_bedroom": "4-bedroom splits",
        }
        parts: list[str] = []
        unpriced: list[str] = []
        for path in self.enabled_paths:
            budget = self.budgets.get(path)
            if budget is None:
                # Ticked, with the price still blank. Named below rather than
                # dropped: it is the one answer that stops a deal saving.
                unpriced.append(labels[path])
                continue
            if path in SPLIT_PATHS:
                parts.append(
                    f"{labels[path]} up to ${budget.total_maximum:,} total "
                    f"(${budget.maximum_monthly:,} each for {budget.occupants})"
                )
            else:
                parts.append(f"{labels[path]} up to ${budget.maximum_monthly:,}")

        sentences: list[str] = []
        if parts:
            sentences.append("Look for " + "; ".join(parts) + ".")
        if unpriced:
            # Every size label is already plural, so the verb is too however
            # many are listed.
            sentences.append(
                _join_plainly(unpriced).capitalize()
                + " are ticked without a monthly maximum, so nothing is searched "
                "for them until you set one."
            )
        if not parts:
            if not unpriced:
                return "Your deal is still a draft. Choose a home type, budget, and area to begin."
            return " ".join(sentences)

        if self.anywhere_in_sf:
            sentences.append("Any San Francisco neighborhood counts.")
        else:
            # Named in full. Truncating at four silently dropped a fifth Dream
            # area from the description of a deal that still searched for it.
            ranked = [
                _join_plainly(list(self.areas.get(tier, ())))
                for tier in ("dream", "strong", "okay")
            ]
            wanted = [text for text in ranked if text]
            # A sentence each, because chaining fifteen names through two
            # "then"s is one breath nobody takes. The full stops also show
            # where one group ends and the next begins.
            for position, text in enumerate(wanted):
                sentences.append(("Prioritize " if position == 0 else "Then ") + text + ".")
            avoid = list(self.areas.get("avoid", ()))
            if avoid:
                verb = "stays" if len(avoid) == 1 else "stay"
                sentences.append(
                    f"{_join_plainly(avoid)} {verb} in near matches, whatever else fits."
                )

        if self.move_in_flexible:
            sentences.append("Move whenever the right place comes up.")
        else:
            earliest = _plain_date(self.earliest_move_in) if self.earliest_move_in else ""
            wanted_by = _plain_date(self.preferred_by) if self.preferred_by else ""
            if earliest and wanted_by:
                sentences.append(f"Move in no earlier than {earliest}, ideally by {wanted_by}.")
            elif earliest:
                sentences.append(f"Move in no earlier than {earliest}.")
            elif wanted_by:
                sentences.append(f"Be in by {wanted_by}, ideally.")

        lease = _plain_lease(self.lease_min_months, self.lease_max_months)
        if lease:
            sentences.append(lease)
        if self.household_maximum:
            sentences.append(f"In a shared home, no more than {self.household_maximum} people in total.")

        important = [name.replace("_", " ") for name, level in self.preferences.items() if level == "important"]
        nice = [name.replace("_", " ") for name, level in self.preferences.items() if level == "nice"]
        sentence = " ".join(sentences)
        if important:
            sentence += " Important: " + ", ".join(important) + "."
        if nice:
            sentence += " Nice to have: " + ", ".join(nice) + "."
        return sentence

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "enabled_paths": list(self.enabled_paths),
            "budgets": {path: self.budgets[path].to_dict() for path in self.enabled_paths if path in self.budgets},
            "geography": {
                "anywhere_in_sf": self.anywhere_in_sf,
                "dream": list(self.areas.get("dream", ())),
                "strong": list(self.areas.get("strong", ())),
                "okay": list(self.areas.get("okay", ())),
                "avoid": list(self.areas.get("avoid", ())),
            },
            "move_in": {
                "flexible": self.move_in_flexible,
                "earliest": self.earliest_move_in,
                "preferred_by": self.preferred_by,
            },
            "lease": {
                "minimum_months": self.lease_min_months,
                "maximum_months": self.lease_max_months,
            },
            "room_household": {
                "private_room_required": self.private_room_required,
                "maximum_people": self.household_maximum,
            },
            "preferences": dict(sorted(self.preferences.items())),
        }
        return result

    @classmethod
    def blank(cls) -> "DealProfile":
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "DealProfile":
        enabled = _clean_strings(data.get("enabled_paths", ()) if isinstance(data.get("enabled_paths"), list) else ())
        raw_budgets = data.get("budgets") if isinstance(data.get("budgets"), dict) else {}
        budgets = {
            path: PathBudget.from_dict(raw_budgets[path], path)
            for path in enabled
            if isinstance(raw_budgets.get(path), dict)
        }
        geography = data.get("geography") if isinstance(data.get("geography"), dict) else {}
        move_in = data.get("move_in") if isinstance(data.get("move_in"), dict) else {}
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else {}
        household = data.get("room_household") if isinstance(data.get("room_household"), dict) else {}
        raw_preferences = data.get("preferences") if isinstance(data.get("preferences"), dict) else {}
        profile = cls(
            state=str(data.get("state") or "draft"),
            enabled_paths=enabled,
            budgets=budgets,
            anywhere_in_sf=geography.get("anywhere_in_sf") is True,
            areas={
                tier: _clean_strings(geography.get(tier, ()) if isinstance(geography.get(tier), list) else ())
                for tier in AREA_TIERS
            },
            move_in_flexible=move_in.get("flexible", True) is True,
            earliest_move_in=str(move_in.get("earliest") or "") or None,
            preferred_by=str(move_in.get("preferred_by") or "") or None,
            lease_min_months=_positive_int(lease.get("minimum_months"), "minimum lease", required=False),
            lease_max_months=_positive_int(lease.get("maximum_months"), "maximum lease", required=False),
            private_room_required=household.get("private_room_required", True) is True,
            household_maximum=_positive_int(household.get("maximum_people"), "maximum household", required=False),
            preferences={str(key): str(value) for key, value in raw_preferences.items()},
        )
        profile.validate()
        return profile

    @classmethod
    def from_legacy(cls, data: Mapping[str, Any]) -> "DealProfile":
        enabled: list[str] = []
        budgets: dict[str, PathBudget] = {}
        room = data.get("budget") if isinstance(data.get("budget"), dict) else {}
        if data.get("private_room", bool(room)) and room.get("max_monthly"):
            enabled.append("private_room")
            budgets["private_room"] = PathBudget(
                maximum_monthly=int(room["max_monthly"]),
                ideal_monthly=int(room["ideal_monthly"]) if room.get("ideal_monthly") else None,
                minimum_monthly=int(room["min_monthly"]) if room.get("min_monthly") else None,
                preferred_minimum=int(room["sweet_spot_min"]) if room.get("sweet_spot_min") else None,
                preferred_maximum=int(room["sweet_spot_max"]) if room.get("sweet_spot_max") else None,
            )
        whole = data.get("whole_unit") if isinstance(data.get("whole_unit"), dict) else {}
        # Before versioned profiles the dashboard and scanners exposed these
        # paths even when their sections were omitted. Preserve that effective
        # behavior during migration; an explicit false still disables a path.
        if whole.get("enabled", True) is True:
            for path in whole.get("unit_types", ["studio", "one_bedroom"]):
                if path in {"studio", "one_bedroom"}:
                    enabled.append(path)
                    budgets[path] = PathBudget(
                        maximum_monthly=int(whole.get("max_monthly", 3000)),
                        maximum_building_units=_unit_ceiling(whole.get("max_building_units", 50)),
                    )
        for path in SPLIT_PATHS:
            section = data.get(path)
            settings = section if isinstance(section, dict) else {}
            # The two- and three-bedroom paths predate versioned profiles, so an
            # omitted section still means enabled. Four-bedroom did not exist
            # then, so a profile that never mentioned it must not silently gain
            # a fourth search: it is enabled only when explicitly present.
            default_enabled = path in ("two_bedroom", "three_bedroom")
            if settings.get("enabled", default_enabled) is True:
                enabled.append(path)
                budgets[path] = PathBudget(
                    maximum_monthly=int(settings.get("max_per_person", DEFAULT_PER_PERSON[path])),
                    occupants=int(settings.get("occupants", DEFAULT_OCCUPANTS[path])),
                    maximum_building_units=_unit_ceiling(settings.get("max_building_units", 50)),
                )
        availability = data.get("availability") if isinstance(data.get("availability"), dict) else {}
        lease = data.get("lease") if isinstance(data.get("lease"), dict) else {}
        household = data.get("household") if isinstance(data.get("household"), dict) else {}
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        areas = {
            "dream": _clean_strings(data.get("ideal_neighborhoods", [])),
            "strong": _clean_strings(data.get("preferred_neighborhoods", [])),
            "okay": _clean_strings(data.get("acceptable_neighborhoods", [])),
            "avoid": (),
        }
        likely_active = bool(enabled) and bool(areas["dream"] or areas["strong"] or areas["okay"])
        profile = cls(
            state="active" if likely_active else "draft",
            enabled_paths=tuple(dict.fromkeys(enabled)),
            budgets=budgets,
            areas=areas,
            move_in_flexible=not bool(availability),
            earliest_move_in=str(availability.get("preferred_by") or "") or None,
            preferred_by=str(availability.get("latest_by") or "") or None,
            lease_min_months=_positive_int(lease.get("min_months"), "minimum lease", required=False),
            lease_max_months=_positive_int(lease.get("max_months"), "maximum lease", required=False),
            private_room_required=data.get("private_room", True) is True,
            household_maximum=_positive_int(household.get("max_people"), "maximum household", required=False),
            preferences={
                name: "important" if features.get(name) is True else "ignore"
                for name in ("sunlight", "park_access", "outdoor_space")
            },
        )
        profile.validate()
        return profile


def technical_settings(data: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("technical"), dict):
        return dict(data["technical"])
    profile_keys = {
        "budget",
        "whole_unit",
        "two_bedroom",
        "three_bedroom",
        "four_bedroom",
        "ideal_neighborhoods",
        "preferred_neighborhoods",
        "acceptable_neighborhoods",
        "private_room",
        "lease",
        "availability",
        "household",
        "features",
    }
    # The canonical keys are the profile itself, never technical settings.
    # Carrying them through put "profile_version" into the legacy view, and
    # parse_preferences takes the profile branch whenever it sees that key, so a
    # document with no "technical" section recursed until the interpreter gave
    # up. Anything written by this app has the section; a hand-edited file or
    # one from an older tool did not.
    return {
        key: value
        for key, value in data.items()
        if key not in profile_keys and key not in {"profile", "profile_version"}
    }


def canonical_document(profile: DealProfile, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    profile.validate()
    return {
        "profile_version": PROFILE_VERSION,
        "profile": profile.to_dict(),
        "technical": technical_settings(previous or {}),
    }


def legacy_view(profile: DealProfile, technical: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = dict(technical or {})
    result.setdefault("minimum_score", 60)
    importance_weights = {"must_have": 12, "important": 8, "nice": 3, "ignore": 0, "avoid": 8}
    feature_levels = {
        "sunlight": profile.preferences.get("natural_light", "ignore"),
        "outdoor_space": profile.preferences.get("outdoor_space", "ignore"),
        "laundry": profile.preferences.get("laundry", "ignore"),
        "furnished": profile.preferences.get("furnished", "ignore"),
        "pets": profile.preferences.get("pets", "ignore"),
        "parking": profile.preferences.get("parking", "ignore"),
    }
    result["weights"] = {
        "price": 25,
        "neighborhood": 30,
        "private_room": 12 if "private_room" in profile.enabled_paths else 0,
        "property_type": 5 if "private_room" in profile.enabled_paths else 0,
        "lease": 8 if profile.lease_min_months or profile.lease_max_months else 0,
        "availability": 8 if not profile.move_in_flexible else 0,
        "household": 5 if profile.household_maximum and "private_room" in profile.enabled_paths else 0,
        "sunlight": importance_weights[feature_levels["sunlight"]],
        "park_access": 0,
        "outdoor_space": importance_weights[feature_levels["outdoor_space"]],
        "laundry": importance_weights[feature_levels["laundry"]],
        "furnished": importance_weights[feature_levels["furnished"]],
        "pets": importance_weights[feature_levels["pets"]],
        "parking": importance_weights[feature_levels["parking"]],
        "lifestyle": 0,
    }
    # Craigslist's search page carries about 220 results and ignores an offset,
    # so 120 was throwing away roughly a hundred homes per search for no reason
    # anyone chose. 250 takes the page as it comes; scoring them is cheap.
    result.setdefault("sources", {"max_results_per_source": 2500, "craigslist_detail_pages_per_scan": 10})
    result["ideal_neighborhoods"] = list(profile.areas.get("dream", ()))
    result["preferred_neighborhoods"] = list(profile.areas.get("strong", ()))
    result["acceptable_neighborhoods"] = list(profile.areas.get("okay", ()))
    result["avoided_neighborhoods"] = list(profile.areas.get("avoid", ()))
    if profile.anywhere_in_sf:
        result["anywhere_in_sf"] = True

    room = profile.budgets.get("private_room")
    result["private_room"] = "private_room" in profile.enabled_paths and profile.private_room_required
    if room:
        ideal = room.ideal_monthly or room.maximum_monthly
        # The sweet spot runs up to the ideal, not at it. Setting both ends to
        # the same number meant only a home priced at exactly $2,500 counted as
        # ideal; $2,499 scored the same 0.72 as one at the very top of the
        # budget, and a genuinely cheap room was never treated as a find. Below
        # the stated floor a home is already out of range, so the floor is the
        # bottom of the band and nothing below it is silently promoted.
        result["budget"] = {
            "max_monthly": room.maximum_monthly,
            "sweet_spot_min": room.preferred_minimum or room.minimum_monthly or 1,
            "sweet_spot_max": room.preferred_maximum or ideal,
            "flexible_margin_percent": 0.05,
        }
        # Only an ideal the reader actually named. Falling back to the maximum
        # let the page tell them $3,500 was "exactly your ideal price" when
        # $3,500 was the most they were willing to pay.
        if room.ideal_monthly is not None:
            result["budget"]["ideal_monthly"] = room.ideal_monthly
        # Only a minimum the user actually stated, and the key is left out
        # entirely rather than set to None, because every reader of it does
        # .get("min_monthly", <default>) and a present-but-None key defeats the
        # default. Deriving a floor from the ceiling meant raising your budget
        # hid cheap homes: a $5,000 cap invented a $1,750 floor, which went to
        # Craigslist as min_price and cut its room search from 241 results to
        # 49. The deal form has no minimum field, so nobody could see or undo it.
        if room.minimum_monthly is not None:
            result["budget"]["min_monthly"] = room.minimum_monthly

    # Only the paths that have a budget: a draft is allowed to hold a ticked
    # size with the price still blank, and every line below reads a budget. The
    # ceiling, the unit types and the per-path maximums have to agree on which
    # paths they are describing -- one of them reading the ticked list while the
    # others read the priced list crashed the live preview with a KeyError the
    # moment a second size was ticked, which froze the review sentence and the
    # shortlist count behind it until a price was typed.
    whole_paths = [
        path
        for path in ("studio", "one_bedroom")
        if path in profile.enabled_paths and path in profile.budgets
    ]
    whole_budgets = [profile.budgets[path] for path in whole_paths]
    if whole_budgets:
        # Existing adapters accept one whole-unit ceiling. Use the highest
        # acceptable cap for collection; scoring checks the path-specific cap.
        result["whole_unit"] = {
            "enabled": True,
            "max_monthly": max(item.maximum_monthly for item in whole_budgets),
            "unit_types": whole_paths,
            # One path saying "no limit" makes the collection ceiling no
            # limit: it is the most permissive of the enabled paths, and
            # scoring still checks each path's own answer.
            "max_building_units": (
                None
                if any(item.maximum_building_units is None for item in whole_budgets)
                else max(item.maximum_building_units for item in whole_budgets)
            ),
        }
        result["path_maximums"] = {
            path: profile.budgets[path].maximum_monthly for path in whole_paths
        }
    else:
        result["whole_unit"] = {
            "enabled": False,
            "max_monthly": 1,
            "unit_types": ["studio"],
            "max_building_units": 50,
        }

    for path in SPLIT_PATHS:
        budget = profile.budgets.get(path)
        result[path] = (
            {
                "enabled": True,
                "occupants": budget.occupants,
                "max_per_person": budget.maximum_monthly,
                "max_building_units": budget.maximum_building_units,
            }
            if budget and path in profile.enabled_paths
            else {
                "enabled": False,
                "occupants": DEFAULT_OCCUPANTS[path],
                "max_per_person": 1,
                "max_building_units": 50,
            }
        )

    result["lease"] = {
        "min_months": profile.lease_min_months or 1,
        "ideal_min_months": profile.lease_min_months or 1,
        "ideal_max_months": profile.lease_max_months or 12,
        "max_months": profile.lease_max_months or 12,
        "flexible": profile.lease_min_months is None and profile.lease_max_months is None,
    }
    result["availability"] = {
        "preferred_by": profile.earliest_move_in,
        "latest_by": profile.preferred_by,
    }
    result["household"] = {
        "min_people": 1,
        "ideal_people": min(profile.household_maximum or 3, 3),
        "max_people": profile.household_maximum or 20,
    }
    result["features"] = {
        "sunlight": feature_levels["sunlight"] in {"must_have", "important", "nice"},
        "park_access": False,
        "outdoor_space": feature_levels["outdoor_space"] in {"must_have", "important", "nice"},
        "laundry": feature_levels["laundry"] in {"must_have", "important", "nice"},
        "furnished": feature_levels["furnished"] in {"must_have", "important", "nice"},
        "pets": feature_levels["pets"] in {"must_have", "important", "nice"},
        "parking": feature_levels["parking"] in {"must_have", "important", "nice"},
    }
    return result


def deal_profile_from_form(form: Any, *, state: str = "active") -> DealProfile:
    """Build the canonical deal from Starlette FormData or a compatible mapping."""

    def values(name: str) -> list[str]:
        if hasattr(form, "getlist"):
            return [str(value) for value in form.getlist(name)]
        value = form.get(name)
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [str(value)] if value not in (None, "") else []

    def value(name: str, default: object = "") -> object:
        return form.get(name, default)

    # One question, not one per path. Nobody wants a different building-size
    # answer for a studio than for a four-bedroom split, and asking six times
    # would be six chances to disagree with yourself. The per-path field is
    # still read when the shared one is absent, so an older form still posts.
    shared_units = value("building_units") if "building_units" in form else None

    enabled = tuple(path for path in HOUSING_PATHS if path in set(values("housing_paths")))
    budgets: dict[str, PathBudget] = {}
    for path in enabled:
        raw_maximum = value(f"{path}_maximum")
        if raw_maximum in (None, "") and state == "draft":
            continue
        maximum = int(_positive_int(raw_maximum, f"{path} maximum"))
        ideal = _positive_int(value(f"{path}_ideal"), f"{path} ideal", required=False)
        minimum = _positive_int(value(f"{path}_minimum"), f"{path} minimum", required=False)
        occupants = (
            int(_positive_int(value(f"{path}_occupants", DEFAULT_OCCUPANTS[path]), f"{path} occupants"))
            if path in SPLIT_PATHS
            else 1
        )
        building_limit = (
            _unit_ceiling(shared_units if shared_units is not None else value(f"{path}_building_units", 50))
            if path != "private_room"
            else None
        )
        budgets[path] = PathBudget(
            maximum_monthly=maximum,
            ideal_monthly=ideal,
            minimum_monthly=minimum,
            occupants=occupants,
            maximum_building_units=building_limit,
        )

    anywhere = str(value("anywhere_in_sf", "")).casefold() in {"1", "true", "on", "yes"}
    areas = {tier: _clean_strings(values(f"areas_{tier}")) for tier in AREA_TIERS}
    flexible = str(value("move_in_flexible", "")).casefold() in {"1", "true", "on", "yes"}
    importance = {
        feature: str(value(f"preference_{feature}", "ignore"))
        # The four a listing states often enough to rank on. Natural light and
        # outdoor space are named in prose too rarely to order anything by, so
        # asking about them bought nothing; a stored answer for either is
        # dropped the next time the deal is saved.
        for feature in (
            "laundry",
            "furnished",
            "pets",
            "parking",
        )
    }
    profile = DealProfile(
        state=state,
        enabled_paths=enabled,
        budgets=budgets,
        anywhere_in_sf=anywhere,
        areas=areas,
        move_in_flexible=flexible,
        earliest_move_in=None if flexible else str(value("earliest_move_in") or "") or None,
        preferred_by=None if flexible else str(value("preferred_by") or "") or None,
        lease_min_months=_positive_int(value("lease_min_months"), "minimum lease", required=False),
        lease_max_months=_positive_int(value("lease_max_months"), "maximum lease", required=False),
        private_room_required=True,
        household_maximum=_positive_int(value("household_maximum"), "maximum household", required=False),
        preferences=importance,
    )
    profile.validate(require_active=state == "active")
    return profile


def profile_form_values(profile: DealProfile) -> dict[str, Any]:
    return {
        "enabled_paths": set(profile.enabled_paths),
        # The single answer the building-size control shows. Whole-home paths
        # share it; a profile that somehow disagrees with itself shows the
        # most permissive of them rather than picking a winner silently.
        "building_units": _shared_unit_ceiling(profile),
        "budgets": {
            path: {
                "maximum": budget.maximum_monthly,
                "minimum": budget.minimum_monthly or "",
                "ideal": budget.ideal_monthly or "",
                "occupants": budget.occupants,
                "building_units": budget.maximum_building_units,
                # Rendered as one control, so the form needs one answer.
                "total_maximum": budget.total_maximum,
            }
            for path, budget in profile.budgets.items()
        },
        "anywhere_in_sf": profile.anywhere_in_sf,
        "areas": profile.areas,
        "move_in_flexible": profile.move_in_flexible,
        "earliest_move_in": profile.earliest_move_in or "",
        "preferred_by": profile.preferred_by or "",
        "lease_min_months": profile.lease_min_months or "",
        "lease_max_months": profile.lease_max_months or "",
        "household_maximum": profile.household_maximum or "",
        "preferences": profile.preferences,
        "summary": profile.summary(),
        "active": profile.active,
    }
