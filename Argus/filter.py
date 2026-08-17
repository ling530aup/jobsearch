"""Case-insensitive fuzzy filters driven by the profile YAML configuration."""

from difflib import SequenceMatcher
import re
from typing import List, Set, Tuple

from .location import CountryLocationMatcher, normalize_location


class JobFilter:
    """Filter jobs by matching each job title against configured title phrases."""

    def __init__(self, target_titles: List[str], min_score: float = 0.8,
                 exclude_levels: List[str] = None):
        """Initialize filter with target job titles.

        Args:
            target_titles: List of job titles to match against.
            min_score: Minimum similarity score (0-1) to consider a match.
            exclude_levels: List of levels to exclude (e.g., ['staff', 'principal']).
        """
        self.target_titles = [str(title).strip() for title in target_titles if str(title).strip()]
        self.min_score = min_score
        self.exclude_levels = [str(level).strip() for level in (exclude_levels or []) if str(level).strip()]
        self._excluded_tokens = self._build_excluded_tokens()
        self._parsed_targets = [self._parse_title(title) for title in self.target_titles]

    def _build_excluded_tokens(self) -> Set[str]:
        """Build exclusion tokens directly from configured level values."""
        excluded = set()
        for level in self.exclude_levels:
            excluded.update(self._normalize(level))
        return excluded

    def _has_excluded_level(self, job_title: str) -> bool:
        """Check if job title contains an excluded level."""
        if not self._excluded_tokens:
            return False
        tokens = self._normalize(job_title)
        return bool(tokens & self._excluded_tokens)

    def _normalize(self, text: str) -> Set[str]:
        """Convert text to a case-insensitive token set."""
        text = str(text).casefold()
        text = re.sub(r'[^\w\s-]', ' ', text)
        return set(text.split())

    def _parse_title(self, title: str) -> dict:
        """Store normalized text and tokens for configuration-driven comparison."""
        normalized = self._normalize_text(title)
        return {"text": normalized, "tokens": set(normalized.split())}

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize case and punctuation without introducing domain assumptions."""
        normalized = str(text).casefold()
        normalized = re.sub(r"[^\w\s]+", " ", normalized)
        return " ".join(normalized.split())

    def _matches_target(self, job_parsed: dict, target_parsed: dict) -> Tuple[bool, float]:
        """Match a configured phrase as a whole phrase or a fuzzy token window."""
        target_text = target_parsed["text"]
        job_text = job_parsed["text"]
        if not target_text or not job_text:
            return False, 0.0

        # Extra seniority, team, or location words around a configured phrase are
        # allowed; the configured phrase itself remains the source of truth.
        if target_text in job_text:
            return True, 1.0

        target_tokens = target_parsed["tokens"]
        job_tokens = job_parsed["tokens"]
        # A configured title's complete token set is an exact semantic match,
        # even when the job title has additional words or changes token order.
        # This path deliberately does not depend on min_score.
        if target_tokens <= job_tokens:
            return True, 1.0

        job_words = job_text.split()
        target_length = len(target_text.split())
        windows = (
            " ".join(job_words[index:index + target_length])
            for index in range(len(job_words) - target_length + 1)
        )
        score = max((SequenceMatcher(None, target_text, window).ratio() for window in windows), default=0.0)
        # The configurable score is only for typo/format tolerance when the
        # configured title's words do not all occur in the job title.
        fuzzy_threshold = max(self.min_score, 0.8)
        return score >= fuzzy_threshold, score

    def matches(self, job_title: str) -> Tuple[bool, float]:
        """Check if a job title matches any target title.

        Args:
            job_title: The job title to check.

        Returns:
            Tuple of (is_match, best_score)
        """
        # Check for excluded levels first
        if self._has_excluded_level(job_title):
            return False, 0.0

        job_parsed = self._parse_title(job_title)
        best_score = 0.0
        best_match = False

        for target_parsed in self._parsed_targets:
            is_match, score = self._matches_target(job_parsed, target_parsed)
            if score > best_score:
                best_score = score
                best_match = is_match

        return best_match, best_score

    def filter_jobs(self, jobs: List) -> List:
        """Filter a list of jobs by title matching.

        Args:
            jobs: List of Job objects.

        Returns:
            List of matching Job objects.
        """
        matching = []
        for job in jobs:
            is_match, score = self.matches(job.title)
            if is_match:
                matching.append(job)
        return matching


class LocationFilter:
    """Filter jobs by explicit locations, configured countries, or remote status."""

    def __init__(self, locations: List[str], allow_remote: bool = True):
        """Initialize a location filter from profile values only."""
        self.locations = [normalize_location(loc) for loc in locations if normalize_location(loc)]
        self.allow_remote = allow_remote
        self._location_patterns = set(self.locations)
        self.country_matcher = CountryLocationMatcher(self.locations)

    def _is_remote(self, location: str) -> bool:
        """Check remote only when the profile explicitly enables it."""
        return self.allow_remote and "remote" in normalize_location(location)

    def _matches_location(self, location: str) -> bool:
        """Apply country boundary, city, remote, and explicit-value checks."""
        if not location:
            return False

        location_lower = normalize_location(location)
        # An explicitly remote role is eligible regardless of the office or
        # payroll country also shown by the ATS (for example "Georgia; Remote").
        if self._is_remote(location_lower):
            return True
        if self.country_matcher.has_conflicting_country(location_lower):
            return False

        if self.country_matcher.matches_city(location_lower):
            return True

        for pattern in self._location_patterns:
            # Short values such as HK must be whole tokens to avoid false hits.
            if len(pattern) <= 3:
                if re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", location_lower):
                    return True
            elif pattern in location_lower or self._fuzzy_location_match(pattern, location_lower):
                return True
        return False

    @staticmethod
    def _fuzzy_location_match(pattern: str, location: str) -> bool:
        """Allow minor spelling variations for explicit non-country values."""
        pattern_words = pattern.split()
        location_words = location.split()
        width = len(pattern_words)
        windows = (
            " ".join(location_words[index:index + width])
            for index in range(len(location_words) - width + 1)
        )
        return any(
            SequenceMatcher(None, pattern, window).ratio() >= 0.85
            for window in windows
        )

    def matches(self, job_location: str) -> bool:
        """Return whether a job location satisfies the configured filter."""
        if not job_location:
            return False
        return self._matches_location(job_location)

    def filter_jobs(self, jobs: List) -> List:
        """Filter a list of jobs by location.

        Args:
            jobs: List of Job objects.

        Returns:
            List of matching Job objects.
        """
        return [job for job in jobs if self.matches(job.location or "")]
