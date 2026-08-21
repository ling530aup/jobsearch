"""Country-aware location matching for job filters."""

from functools import lru_cache
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
        "usa": "US",
        "u s": "US",
        "u s a": "US",
        "uae": "AE",
        "u a e": "AE",
    }
    # ATS feeds often use state/territory abbreviations or regional labels
    # instead of appending a country, especially Workday feeds for Australia.
    # These are geographic labels, not ISO country codes (for example, SA is
    # South Australia here, not Saudi Arabia).
    REGION_ALIASES = {
        "nsw": "AU",
        "new south wales": "AU",
        "vic": "AU",
        "victoria": "AU",
        "qld": "AU",
        "queensland": "AU",
        "sa": "AU",
        "south australia": "AU",
        "wa": "AU",
        "western australia": "AU",
        "tas": "AU",
        "tasmania": "AU",
        "nt": "AU",
        "northern territory": "AU",
        "act": "AU",
        "australian capital territory": "AU",
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
        job_country_codes = self._country_codes_in_location(str(location))
        return bool(
            self._configured_country_codes
            and job_country_codes
            and not job_country_codes & self._configured_country_codes
        )

    def matches_country(self, location: str) -> bool:
        """Return True when the job explicitly names a configured country."""
        if not self._configured_country_codes:
            return False
        job_country_codes = self._country_codes_in_location(str(location))
        return bool(job_country_codes & self._configured_country_codes)

    @classmethod
    def is_country_name(cls, location: str) -> bool:
        """Return whether one configured value resolves to a country."""
        return bool(cls._resolve_country_codes([normalize_location(location)]))

    def matches_city(self, location: str) -> bool:
        """Match a city that belongs to at least one configured country.

        An explicit country in a job location is checked separately by
        ``has_conflicting_country``. For city-only locations, accepting a city
        in the configured country avoids rejecting legitimate places such as
        Dublin merely because a same-named place exists elsewhere.
        """
        if not self._country_city_names:
            return False
        # A comma/semicolon separated component is a geographic unit. Checking
        # arbitrary token windows made "New York" match the UK city "York" and
        # was the main source of cross-country false positives.
        components = re.split(r"[,;|\n]+", str(location))
        for component in components:
            normalized = normalize_location(component)
            if not normalized:
                continue
            if self._matches_city_component(normalized):
                return True
        return False

    def _matches_city_component(self, component: str) -> bool:
        """Match a city at the start of an ATS region label.

        This handles values such as ``Sydney CBD Area`` and ``SA Adelaide
        CBD Area`` while deliberately avoiding arbitrary token-window matches
        such as treating the York in ``New York`` as a UK location.
        """
        tokens = component.split()
        if tokens and tokens[0] in self.REGION_ALIASES:
            tokens = tokens[1:]
        if not tokens:
            return False

        for width in range(min(3, len(tokens)), 0, -1):
            candidate = " ".join(tokens[:width])
            if candidate in self._country_city_names:
                # Avoid very common short words that happen to be city names.
                if width > 1 or len(candidate) >= 4:
                    return True
        return False

    @classmethod
    @lru_cache(maxsize=8192)
    def _country_codes_in_location(cls, location: str) -> Set[str]:
        """Find explicit country names/codes in a job's location string."""
        country_index, _ = cls._load_country_city_index()
        normalized = normalize_location(location)
        codes = set()
        matches = []
        for country_name, code in country_index.items():
            if len(country_name) <= 3:
                # ISO codes are conventionally uppercase. Matching them after
                # case-folding turns common words such as Spanish "de" into
                # Germany (DE) and English "in" into India (IN).
                raw_match = re.search(
                    rf"(?<!\w){re.escape(country_name.upper())}(?!\w)",
                    str(location),
                )
                if raw_match:
                    region_code = cls.REGION_ALIASES.get(country_name)
                    matches.append((
                        raw_match.start(),
                        raw_match.end(),
                        region_code or code,
                    ))
                continue
            match = re.search(
                rf"(?<!\w){re.escape(country_name)}(?!\w)",
                normalized,
            )
            if match:
                matches.append((match.start(), match.end(), code))

        for alias, code in cls.COUNTRY_ALIASES.items():
            match = re.search(
                rf"(?<!\w){re.escape(alias)}(?!\w)",
                normalized,
            )
            if match:
                matches.append((match.start(), match.end(), code))

        for region, code in cls.REGION_ALIASES.items():
            match = re.search(
                rf"(?<!\w){re.escape(region)}(?!\w)",
                normalized,
            )
            if match:
                matches.append((match.start(), match.end(), code))

        # Prefer the longest country label at an overlapping position, e.g.
        # Papua New Guinea must not also be interpreted as Guinea.
        for start, end, code in matches:
            if any(
                other_start <= start and other_end >= end
                and other_end - other_start > end - start
                for other_start, other_end, _ in matches
            ):
                continue
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
            code = (
                country_index.get(alias)
                or country_index.get(alias.upper().casefold())
                or cls.REGION_ALIASES.get(alias)
            )
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
