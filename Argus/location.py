"""Country-aware location matching for job filters."""

import re
from typing import List, Set

try:
    import geonamescache
except ImportError:  # City-only filtering remains available without the optional index.
    geonamescache = None


def normalize_location(text: str) -> str:
    """Normalize configured and discovered location values consistently."""
    normalized = str(text).casefold()
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return " ".join(normalized.split())


class CountryLocationMatcher:
    """Resolve configured countries to cities and enforce country boundaries.

    Career pages frequently return only a city. This class owns the optional
    GeoNames-backed city index so the public location filter stays small and
    focused on composing explicit, country, and remote matching.
    """

    # Common non-ISO country labels found in job-search configuration files.
    COUNTRY_ALIASES = {
        "uk": "GB",
        "u k": "GB",
        "great britain": "GB",
        "england": "GB",
        "scotland": "GB",
        "wales": "GB",
        "northern ireland": "GB",
    }
    _CITY_INDEX = None
    _COUNTRY_INDEX = None

    def __init__(self, locations: List[str]):
        """Build a country-aware matcher from configured location values."""
        self.locations = [normalize_location(loc) for loc in locations if normalize_location(loc)]
        self._configured_country_codes = self._resolve_country_codes(self.locations)
        self._country_city_names = self._build_country_city_names(self._configured_country_codes)

    def has_conflicting_country(self, location: str) -> bool:
        """Return True when an explicit job country is not configured."""
        job_country_codes = self._country_codes_in_location(normalize_location(location))
        return bool(job_country_codes and not job_country_codes & self._configured_country_codes)

    def matches_city(self, location: str) -> bool:
        """Match a city that belongs to at least one configured country.

        An explicit country in a job location is checked separately by
        ``has_conflicting_country``. For city-only locations, accepting a city
        in the configured country avoids rejecting legitimate places such as
        Dublin merely because a same-named place exists elsewhere.
        """
        if not self._country_city_names:
            return False
        tokens = normalize_location(location).split()
        # Check token windows rather than scanning every city for every job.
        for width in range(1, len(tokens) + 1):
            for index in range(len(tokens) - width + 1):
                if " ".join(tokens[index:index + width]) in self._country_city_names:
                    return True
        return False

    @classmethod
    def _country_codes_in_location(cls, location: str) -> Set[str]:
        """Find explicit country names/codes in a job's location string."""
        country_index, _ = cls._load_country_city_index()
        codes = set()
        tokens = set(location.split())
        for country_name, code in country_index.items():
            if len(country_name) <= 3:
                if country_name in tokens:
                    codes.add(code)
            elif country_name in location:
                codes.add(code)
        return codes

    @classmethod
    def _load_country_city_index(cls):
        """Load GeoNames data once and keep filtering offline and deterministic."""
        if cls._CITY_INDEX is not None:
            return cls._COUNTRY_INDEX, cls._CITY_INDEX
        if geonamescache is None:
            cls._COUNTRY_INDEX, cls._CITY_INDEX = {}, {}
            return cls._COUNTRY_INDEX, cls._CITY_INDEX

        cache = geonamescache.GeonamesCache()
        countries = cache.get_countries()
        cities = cache.get_cities()
        cls._COUNTRY_INDEX = {
            normalize_location(name): code.upper()
            for code, country in countries.items()
            for name in (country.get("name", ""), code)
            if name
        }
        cls._CITY_INDEX = {}
        for city in cities.values():
            code = str(city.get("countrycode", "")).upper()
            if not code:
                continue
            for name in (city.get("name", ""), city.get("asciiname", "")):
                normalized = normalize_location(name)
                if normalized:
                    cls._CITY_INDEX.setdefault(code, set()).add(normalized)
        return cls._COUNTRY_INDEX, cls._CITY_INDEX

    @classmethod
    def _resolve_country_codes(cls, locations: List[str]) -> Set[str]:
        country_index, _ = cls._load_country_city_index()
        codes = set()
        for location in locations:
            alias = cls.COUNTRY_ALIASES.get(location, location)
            code = country_index.get(alias) or country_index.get(alias.upper().casefold())
            if code:
                codes.add(code)
        return codes

    @classmethod
    def _build_country_city_names(cls, country_codes: Set[str]) -> Set[str]:
        _, city_index = cls._load_country_city_index()
        return {
            city
            for code in country_codes
            for city in city_index.get(code, set())
        }
