"""ATS detection logic."""

import html
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from .base import create_http_client


class ATSDetector:
    """Detects which ATS system a career page uses."""

    ATS_PATTERNS = {
        "greenhouse": [
            r"boards\.greenhouse\.io",
            r"job-boards\.greenhouse\.io",
            r"boards\.greenhouse\.eu",
            r"greenhouse\.io/embed",
            r"api\.greenhouse\.io",
        ],
        "lever": [
            r"jobs\.lever\.co",
            r"lever\.co/embed",
        ],
        "ashby": [
            r"jobs\.ashbyhq\.com",
            r"ashbyhq\.com/api",
        ],
        "workday": [
            r"\.myworkdayjobs\.com",
            r"\.myworkdaysite\.com",
            r"/wday/cxs/",
        ],
        "eightfold": [
            r"\.eightfold\.ai",
            r"portal\.careers\.",
        ],
        "successfactors": [
            r"(?:career\d+|jobs\d*|[a-z0-9-]+)\.successfactors\.(?:com|eu)",
            r"successfactors\.(?:com|eu)/(?:career|portalcareer)",
        ],
        "workable": [r"apply\.workable\.com"],
        "smartrecruiters": [r"(?:jobs|careers)\.smartrecruiters\.com"],
        "teamtailor": [r"\.teamtailor\.com"],
        "icims": [r"\.icims\.com"],
        "oracle": [r"\.oraclecloud\.com/(?:hcmUI|hcmRestApi)"],
        "avature": [r"\.avature\.(?:net|com)"],
        "taleo": [r"\.taleo\.net"],
        "jobvite": [r"jobs\.jobvite\.com"],
        "personio": [r"jobs\.personio\.(?:de|com)"],
        "recruitee": [r"\.recruitee\.com"],
        "bamboohr": [r"\.bamboohr\.com/careers"],
        "tribepad": [r"tribepad"],
    }

    HTML_INDICATORS = {
        "greenhouse": [
            "greenhouse",
            "grnhse_app",
            "greenhouse-job-board",
        ],
        "lever": [
            "lever-jobs-container",
            "lever-job-title",
            "jobs.lever.co",
        ],
        "ashby": [
            "ashby-job-posting",
            "ashbyhq",
        ],
        "workday": [
            "myworkdayjobs",
            "myworkdaysite",
            "/wday/cxs/",
        ],
        "eightfold": [
            "eightfold",
            "smartapplydata",
            "/api/apply/v2/jobs",
        ],
        "successfactors": [
            "successfactors",
            "careerjobsearchcontroller",
            "portalcareer?company=",
        ],
        "workable": ["apply.workable.com"],
        "smartrecruiters": ["smartrecruiters.com"],
        "teamtailor": ["teamtailor.com"],
        "icims": ["icims.com"],
        "oracle": ["candidateexperience", "hcmui/candidateexperience"],
        "avature": ["avature.net", "avature.com"],
        "taleo": ["taleo.net"],
        "jobvite": ["jobs.jobvite.com"],
        "personio": ["jobs.personio"],
        "recruitee": ["recruitee.com"],
        "bamboohr": ["bamboohr.com/careers"],
        "tribepad": ["tribepad"],
    }

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.client = create_http_client(timeout)
        self._response_cache = {}

    def detect(self, career_url: str) -> str:
        """Detect ATS type from career URL.

        Args:
            career_url: The company's career page URL.

        Returns:
            ATS type: a supported ATS name or 'unknown'
        """
        # First check URL patterns
        ats_type = self._check_url_patterns(career_url)
        if ats_type:
            return ats_type

        # Fetch page and check HTML content
        try:
            response = self._get(career_url)
            if response.status_code == 200:
                ats_type = self._check_url_patterns(str(response.url))
                if ats_type:
                    return ats_type

                for candidate_url in self._extract_candidate_urls(response.text, str(response.url)):
                    ats_type = self._check_url_patterns(candidate_url)
                    if ats_type:
                        return ats_type

                ats_type = self._check_html_content(response.text)
                if ats_type:
                    return ats_type

                # Check for iframe sources
                ats_type = self._check_iframe_sources(response.text)
                if ats_type:
                    return ats_type
        except Exception:
            pass

        return "unknown"

    def resolve_url(self, career_url: str, ats_type: str) -> str:
        """Resolve a corporate careers page to its embedded ATS job-board URL."""
        if self._check_url_patterns(career_url) == ats_type:
            return self._normalize_ats_url(career_url, ats_type)
        try:
            response = self._get(career_url)
        except Exception:
            return career_url
        if self._check_url_patterns(str(response.url)) == ats_type:
            return self._normalize_ats_url(str(response.url), ats_type)
        for candidate_url in self._extract_candidate_urls(response.text, str(response.url)):
            if self._check_url_patterns(candidate_url) == ats_type:
                return self._normalize_ats_url(candidate_url, ats_type)
        return career_url

    @staticmethod
    def _normalize_ats_url(url: str, ats_type: str) -> str:
        """Turn a job-detail link into the corresponding public board URL."""
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        keep_query = ats_type in {"successfactors", "eightfold"}
        path = parsed.path

        if ats_type == "workday" and parts:
            board_index = 1 if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) and len(parts) > 1 else 0
            path = "/" + "/".join(parts[:board_index + 1])
        elif ats_type in {
            "greenhouse", "lever", "ashby", "workable", "smartrecruiters"
        } and parts:
            path = "/" + parts[0]

        return urlunparse(parsed._replace(
            path=path,
            params="",
            query=parsed.query if keep_query else "",
            fragment="",
        ))

    def _get(self, url: str):
        response = self._response_cache.get(url)
        if response is None:
            response = self.client.get(url)
            self._response_cache[url] = response
        return response

    @staticmethod
    def _extract_candidate_urls(page_html: str, base_url: str) -> list[str]:
        """Extract link-like URLs from HTML attributes and embedded scripts."""
        decoded = html.unescape(page_html).replace(r"\/", "/")
        candidates = re.findall(
            r'''(?:href|src|action|data-url)\s*=\s*["']([^"']+)["']''',
            decoded,
            re.IGNORECASE,
        )
        candidates.extend(re.findall(r'''https?://[^\s"'<>\\]+''', decoded, re.IGNORECASE))
        urls = []
        seen = set()
        for candidate in candidates:
            url = urljoin(base_url, candidate.strip())
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    @staticmethod
    def _check_url_patterns(url: str) -> Optional[str]:
        """Check URL against known ATS patterns."""
        for ats_type, patterns in ATSDetector.ATS_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url, re.IGNORECASE):
                    return ats_type
        return None

    def _check_html_content(self, html: str) -> Optional[str]:
        """Check HTML content for ATS indicators."""
        html_lower = html.lower()
        for ats_type, indicators in self.HTML_INDICATORS.items():
            for indicator in indicators:
                if indicator.lower() in html_lower:
                    return ats_type
        return None

    def _check_iframe_sources(self, html: str) -> Optional[str]:
        """Check iframe src attributes for ATS URLs."""
        iframe_pattern = r'<iframe[^>]+src=["\']([^"\']+)["\']'
        matches = re.findall(iframe_pattern, html, re.IGNORECASE)
        for src in matches:
            ats_type = self._check_url_patterns(src)
            if ats_type:
                return ats_type
        return None

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self) -> "ATSDetector":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
