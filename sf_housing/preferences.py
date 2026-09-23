from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from .deal_profile import (
    PROFILE_VERSION,
    DealProfile,
    DealProfileError,
    canonical_document,
    legacy_view,
    technical_settings,
)


CRITERIA = {
    "price",
    "neighborhood",
    "private_room",
    "property_type",
    "lease",
    "availability",
    "household",
    "sunlight",
    "park_access",
    "outdoor_space",
    "laundry",
    "furnished",
    "pets",
    "parking",
    "lifestyle",
}


class PreferenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Preferences:
    data: dict[str, Any]
    canonical: dict[str, Any] | None = None
    deal: DealProfile | None = None

    @property
    def deal_profile(self) -> DealProfile:
        return self.deal or DealProfile.from_legacy(self.data)

    @property
    def profile_active(self) -> bool:
        return self.deal_profile.active

    @property
    def minimum_score(self) -> int:
        return int(self.data.get("minimum_score", 60))

    @property
    def weights(self) -> dict[str, float]:
        raw = self.data.get("weights") or {}
        return {key: float(raw.get(key, 0)) for key in CRITERIA}

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def list_value(self, name: str) -> list[str]:
        value = self.data.get(name)
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def mapping_value(self, name: str) -> dict[str, list[str]]:
        value = self.data.get(name)
        if not isinstance(value, dict):
            return {}
        return {
            str(key).strip(): [str(alias).strip() for alias in aliases if str(alias).strip()]
            for key, aliases in value.items()
            if str(key).strip() and isinstance(aliases, list)
        }

    @property
    def profile_incomplete(self) -> bool:
        return not self.profile_active


# A building-size ceiling is the one number here that may be absent on
# purpose: None means "any size", which is a real answer and not a missing one.
def _valid_unit_ceiling(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value > 0)


def parse_preferences(text: str) -> Preferences:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreferenceError(f"Invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PreferenceError("Preferences must be a YAML mapping.")

    if "profile_version" in data:
        if data.get("profile_version") != PROFILE_VERSION:
            raise PreferenceError(
                f"Unsupported profile_version {data.get('profile_version')}; expected {PROFILE_VERSION}."
            )
        profile_data = data.get("profile")
        if not isinstance(profile_data, dict):
            raise PreferenceError("profile must be a mapping.")
        try:
            profile = DealProfile.from_dict(profile_data)
        except DealProfileError as exc:
            raise PreferenceError(str(exc)) from exc
        legacy = legacy_view(profile, technical_settings(data))
        # Reuse the mature legacy validation for ranking/source settings. The
        # stored canonical document remains the source of personal answers;
        # this mapping is only a compatibility view for existing consumers.
        validated = parse_preferences(yaml.safe_dump(legacy, sort_keys=False, allow_unicode=True))
        return Preferences(validated.data, data, profile)

    minimum_score = data.get("minimum_score", 60)
    if not isinstance(minimum_score, (int, float)) or not 0 <= minimum_score <= 100:
        raise PreferenceError("minimum_score must be between 0 and 100.")

    for section_name in (
        "budget",
        "whole_unit",
        "two_bedroom",
        "three_bedroom",
        "lease",
        "availability",
        "household",
        "features",
        "weights",
        "sources",
    ):
        value = data.get(section_name, {})
        if value is not None and not isinstance(value, dict):
            raise PreferenceError(f"{section_name} must be a mapping.")

    weights = data.get("weights") or {}
    unknown_weights = set(weights) - CRITERIA
    if unknown_weights:
        raise PreferenceError(f"Unknown scoring weights: {', '.join(sorted(unknown_weights))}")
    for name, weight in weights.items():
        if not isinstance(weight, (int, float)) or weight < 0:
            raise PreferenceError(f"Weight {name} must be a non-negative number.")
    if weights and sum(float(value) for value in weights.values()) <= 0:
        raise PreferenceError("At least one scoring weight must be positive.")

    for list_name in (
        "ideal_neighborhoods",
        "preferred_neighborhoods",
        "acceptable_neighborhoods",
        "property_types",
        "lifestyle_keywords",
        "dealbreakers",
    ):
        value = data.get(list_name, [])
        if value is not None and not isinstance(value, list):
            raise PreferenceError(f"{list_name} must be a list.")

    aliases = data.get("neighborhood_aliases", {})
    if aliases is not None and not isinstance(aliases, dict):
        raise PreferenceError("neighborhood_aliases must be a mapping of area names to alias lists.")
    if isinstance(aliases, dict) and any(not isinstance(value, list) for value in aliases.values()):
        raise PreferenceError("Each neighborhood_aliases value must be a list.")

    whole_unit = data.get("whole_unit") or {}
    if whole_unit:
        if not isinstance(whole_unit.get("enabled", True), bool):
            raise PreferenceError("whole_unit.enabled must be true or false.")
        for name in ("max_monthly", "max_building_units"):
            value = whole_unit.get(name)
            if name == "max_building_units":
                if not _valid_unit_ceiling(value):
                    raise PreferenceError("whole_unit.max_building_units must be a positive whole number or empty for any size.")
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PreferenceError(f"whole_unit.{name} must be a positive whole number.")
        unit_types = whole_unit.get("unit_types", [])
        if not isinstance(unit_types, list) or not unit_types:
            raise PreferenceError("whole_unit.unit_types must list studio and/or one_bedroom.")
        unknown_types = {str(value) for value in unit_types} - {"studio", "one_bedroom"}
        if unknown_types:
            raise PreferenceError(
                "Unknown whole-unit types: " + ", ".join(sorted(unknown_types))
            )

    two_bedroom = data.get("two_bedroom") or {}
    if two_bedroom:
        if not isinstance(two_bedroom.get("enabled", True), bool):
            raise PreferenceError("two_bedroom.enabled must be true or false.")
        for name in ("occupants", "max_per_person", "max_building_units"):
            value = two_bedroom.get(name)
            if name == "max_building_units":
                # Absent on purpose means "any size"; anything else is still a
                # number, so skipping the check outright would let junk through.
                if not _valid_unit_ceiling(value):
                    raise PreferenceError(
                        f"two_bedroom.max_building_units must be a positive whole number or empty for any size."
                    )
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PreferenceError(f"two_bedroom.{name} must be a positive whole number.")

    three_bedroom = data.get("three_bedroom") or {}
    if three_bedroom:
        if not isinstance(three_bedroom.get("enabled", True), bool):
            raise PreferenceError("three_bedroom.enabled must be true or false.")
        for name in ("occupants", "max_per_person", "max_building_units"):
            value = three_bedroom.get(name)
            if name == "max_building_units":
                # Absent on purpose means "any size"; anything else is still a
                # number, so skipping the check outright would let junk through.
                if not _valid_unit_ceiling(value):
                    raise PreferenceError(
                        f"three_bedroom.max_building_units must be a positive whole number or empty for any size."
                    )
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PreferenceError(f"three_bedroom.{name} must be a positive whole number.")

    try:
        profile = DealProfile.from_legacy(data)
    except DealProfileError as exc:
        raise PreferenceError(str(exc)) from exc
    return Preferences(data, None, profile)


# The same 1.2KB of YAML, parsed again for every request the app serves.
#
# ``parse_preferences`` is not cheap: it validates, it builds the deal profile,
# and for a legacy file it dumps its own legacy view back to YAML and reads it
# again. Measured at 14.6ms, which was half of the Saved page and a tenth of
# the shortlist -- paid on every route, on every navigation.
#
# Keyed on the file's exact bytes rather than its modification time. Content is
# the thing the answer depends on: a save that rewrites the same settings is
# correctly a cache hit, a clock that goes backwards cannot serve a stale
# answer, and a second process writing the file is noticed the moment its text
# differs. The file is still read every time -- that part costs 0.05ms -- so
# nothing here can go stale behind a write. Small, because there is one
# preferences file and the tests use a handful.
@lru_cache(maxsize=8)
def _parsed(text: str) -> Preferences:
    return parse_preferences(text)


def load_preferences(path: Path) -> Preferences:
    if not path.exists():
        raise PreferenceError(f"Preference file does not exist: {path}")
    return _parsed(path.read_text(encoding="utf-8"))


def save_preferences(path: Path, text: str) -> Preferences:
    preferences = parse_preferences(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="preferences-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return preferences


def ensure_preferences(path: Path) -> Preferences:
    """Create a neutral draft for a genuinely new installation."""
    if path.exists():
        return load_preferences(path)
    profile = DealProfile.blank()
    document = canonical_document(profile)
    return save_preferences(
        path,
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100_000),
    )


def setting_int(value: object, default: int) -> int:
    """Read a numeric setting that may be missing, null, or nonsense.

    Readers all wrote ``int(section.get(key, default))``, where the default only
    applies when the key is absent. A key present and null therefore reached
    int() and raised, which on the dashboard is a 500 on the page the whole app
    opens to. A configuration value nobody can parse should fall back, not take
    the page down.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def preferences_with_deal(profile: DealProfile, previous: Preferences | None = None) -> Preferences:
    """The preferences a deal would produce, without writing anything.

    Built through ``canonical_document`` and read back through
    ``parse_preferences`` -- the same two steps saving and loading take -- so a
    deal previewed on the page scores exactly as it will once it is saved. A
    shortcut that assembled the legacy view directly would be a second way of
    turning a deal into preferences, free to drift from the one that counts.
    """
    base = (
        previous.canonical
        if previous is not None and previous.canonical is not None
        else previous.data
        if previous is not None
        else {}
    )
    document = canonical_document(profile, base)
    return parse_preferences(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100_000)
    )


def save_deal_profile(
    path: Path,
    profile: DealProfile,
    previous: Preferences | None = None,
    *,
    technical: Mapping[str, Any] | None = None,
) -> Preferences:
    """Persist one canonical profile while retaining non-personal source settings."""
    profile.validate()
    base = (
        previous.canonical
        if previous is not None and previous.canonical is not None
        else previous.data
        if previous is not None
        else {}
    )
    document = canonical_document(profile, base)
    if technical:
        # The match cut-off is not a personal answer about housing, so it lives
        # with the other technical settings rather than inside the profile.
        document["technical"].update(technical)
    return save_preferences(
        path,
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100_000),
    )
