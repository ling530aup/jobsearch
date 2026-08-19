"""ATS detection logic."""

import html
import re
from collections import Counter
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

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
        "talentbrew": [r"(?:tbcdn\.talentbrew\.com|\.talentbrew\.com)"],
        "apple": [r"jobs\.apple\.com/(?:[a-z]{2}-[a-z]{2}/)?search"],
    }

    HTML_INDICATORS = {
        "greenhouse": [
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
        "talentbrew": ["tbcdn.talentbrew.com", "radancy", "talentbrew"],
        "apple": ["jobs.apple.com"],
    }

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.client = create_http_client(timeout)
        self._response_cache = {}
        self.resolved_url = ""

    def detect(self, career_url: str) -> str:
        """Detect ATS type from career URL.

        Args:
            career_url: The company's career page URL.

        Returns:
            ATS type: a supported ATS name or 'unknown'
        """
        # First check URL patterns
        ats_type = self._check_url_patterns(career_url)
        if ats_type and self.is_public_board_url(career_url, ats_type):
            return ats_type

        # Fetch page and check HTML content
        try:
            response = self._get(career_url)
            # httpx follows 301/302/303/307/308 automatically. Retain the
            # final successful URL so callers do not start another client at
            # the original URL and repeat the redirect chain.
            if response.status_code == 200:
                self.resolved_url = str(response.url)
            if response.status_code == 200:
                ats_type = self._check_url_patterns(str(response.url))
                if ats_type and self.is_public_board_url(str(response.url), ats_type):
                    return ats_type

                # TalentBrew/Radancy pages often contain outbound links to a
                # separate talent network. The page itself remains the full
                # job catalogue and must win over that secondary ATS link.
                if any(
                    indicator in response.text.casefold()
                    for indicator in self.HTML_INDICATORS["talentbrew"]
                ):
                    return "talentbrew"

                candidate = self._best_candidate_url(
                    self._extract_candidate_urls(response.text, str(response.url))
                )
                if candidate:
                    candidate_type = self._check_url_patterns(candidate)
                    if candidate_type != "greenhouse" or self._greenhouse_candidate_is_usable(candidate):
                        return candidate_type or "unknown"

                if re.search(r'''<code[^>]+id=["']pcsx-data["']''', response.text, re.I):
                    return "eightfold"

                ats_type = self._check_html_content(response.text)
                # A bare Eightfold brand/script mention without a valid public
                # board URL is commonly a talent-network widget, not jobs.
                if ats_type == "workday" and "/wday/cxs/" not in response.text.casefold():
                    ats_type = None
                # Explicit Greenhouse markup (grnhse_app/greenhouse-job-board)
                # is already a strong signal. Candidate board URLs were
                # validated in the branch above; validating the entire list
                # again here caused one extra API lookup per embedded link.
                if ats_type and ats_type != "eightfold":
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
        if (
            self._check_url_patterns(career_url) == ats_type
            and self.is_public_board_url(career_url, ats_type)
        ):
            return self._normalize_ats_url(career_url, ats_type)
        try:
            response = self._get(career_url)
        except Exception:
            return career_url
        if (
            self._check_url_patterns(str(response.url)) == ats_type
            and self.is_public_board_url(str(response.url), ats_type)
        ):
            return self._normalize_ats_url(str(response.url), ats_type)
        candidates = [
            candidate_url
            for candidate_url in self._extract_candidate_urls(response.text, str(response.url))
            if self._check_url_patterns(candidate_url) == ats_type
            and self.is_public_board_url(candidate_url, ats_type)
            and (
                ats_type != "greenhouse"
                or self._greenhouse_candidate_is_usable(candidate_url)
            )
        ]
        normalized = {
            self._normalize_ats_url(candidate_url, ats_type)
            for candidate_url in candidates
        }
        # Corporate pages may aggregate several Greenhouse boards. Keep the
        # original page so GreenhouseFetcher can merge every board it exposes.
        if ats_type == "greenhouse" and len(normalized) > 1:
            return career_url
        candidate = self._best_candidate_url(candidates, ats_type)
        if candidate:
            return self._normalize_ats_url(candidate, ats_type)
        return str(response.url) or career_url

    def _greenhouse_candidate_is_usable(self, candidate_url: str) -> bool:
        """Verify that a Greenhouse URL identifies an existing public board.

        Greenhouse links are often left in old career-page markup after a
        company changes ATS. A syntactically valid ``boards-api`` URL is not
        enough: the API returns 404 for retired or guessed board slugs.

        Public ``boards.greenhouse.io`` links are intentionally not probed
        here. The fetcher will make the single required jobs request; probing
        both URL forms during detection needlessly doubles network work. The
        API form is the one that commonly contains a malformed guessed slug.
        """
        if self._check_url_patterns(candidate_url) != "greenhouse":
            return False
        candidate_host = urlparse(str(candidate_url)).netloc.casefold()
        if not candidate_host.startswith("boards-api."):
            return True
        normalized = self._normalize_ats_url(candidate_url, "greenhouse")
        parsed = urlparse(normalized)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] in {"embed", "v1", "boards"}:
            return False
        board_token = parts[0]
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
        try:
            response = self._get(api_url)
            if response.status_code != 200:
                return False
            data = response.json()
            return isinstance(data, dict) and isinstance(data.get("jobs"), list)
        except Exception:
            return False

    @classmethod
    def _best_candidate_url(
        cls,
        candidates: list[str],
        required_type: Optional[str] = None,
    ) -> str:
        """Choose the most frequently referenced valid public ATS board."""
        valid = []
        for candidate in candidates:
            ats_type = cls._check_url_patterns(candidate)
            if not ats_type or (required_type and ats_type != required_type):
                continue
            if not cls.is_public_board_url(candidate, ats_type):
                continue
            normalized = cls._normalize_ats_url(candidate, ats_type)
            valid.append((candidate, ats_type, normalized))
        if not valid:
            return ""
        counts = Counter((ats_type, normalized) for _, ats_type, normalized in valid)
        best_key = max(counts, key=lambda key: counts[key])
        return next(candidate for candidate, ats_type, normalized in valid if (
            ats_type, normalized
        ) == best_key)

    @staticmethod
    def is_public_board_url(url: str, ats_type: Optional[str] = None) -> bool:
        """Reject vendor assets, admin pages and other non-crawlable ATS URLs."""
        url = re.sub(r"\\+/", "/", str(url))
        parsed = urlparse(url)
        host = parsed.netloc.casefold()
        path = parsed.path.casefold()
        detected_type = ats_type or ATSDetector._check_url_patterns(url)
        if not detected_type:
            return False

        blocked_path_parts = (
            "/login", "/sign-in", "/signin", "/my-profile",
            "/privacy", "_privacy", "/legal", "/cookie", "/dashboard",
        )
        blocked_extensions = (
            ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico",
            ".webp", ".gif", ".woff", ".woff2",
        )
        if any(part in path for part in blocked_path_parts) or path.endswith(blocked_extensions):
            return False

        # Vendor home pages and authenticated Teamtailor administration URLs
        # identify the product but not a company's public job board.
        if detected_type == "teamtailor":
            return host not in {"www.teamtailor.com", "app.teamtailor.com", "teamtailor.com"}
        if detected_type == "icims":
            return (
                not host.startswith("cookie-policy-scripts.")
                and host != "www.icims.com"
                and "/icims2/servlet" not in path
            )
        if detected_type == "greenhouse":
            parts = [part for part in path.split("/") if part]
            if host.startswith("boards-api."):
                return len(parts) >= 3 and parts[:2] == ["v1", "boards"]
            return bool(parts and parts[0] not in {"embed", "v1"})
        if detected_type == "workable":
            parts = [part for part in path.split("/") if part]
            return bool(parts and parts[0] != "j")
        if detected_type == "workday" and "myworkdaysite.com" in host:
            parts = [part for part in path.split("/") if part]
            return not (parts == ["recruiting"])
        if detected_type == "successfactors":
            return (
                not host.startswith("rmkcdn.")
                and "navbarlevel=my_profile" not in parsed.query.casefold()
                and "loginflowrequired=true" not in parsed.query.casefold()
                and (
                    "/career" in path
                    or "/portalcareer" in path
                    or "company=" in parsed.query.casefold()
                    or "career_company=" in parsed.query.casefold()
                )
            )
        if detected_type == "eightfold":
            if host in {"app.eightfold.ai", "www.eightfold.ai", "eightfold.ai"}:
                return False
            if "/careers/join" in path:
                return bool(parse_qs(parsed.query).get("domain"))
            return "/careers" in path
        if detected_type == "talentbrew":
            return not host.startswith("tbcdn.")
        if detected_type == "taleo":
            return not path.endswith("/profile.ftl")
        return True

    @staticmethod
    def _normalize_ats_url(url: str, ats_type: str) -> str:
        """Turn a job-detail link into the corresponding public board URL."""
        url = re.sub(r"\\+/", "/", str(url))
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        keep_query = ats_type in {"successfactors", "eightfold"}
        path = parsed.path

        if ats_type == "workday" and len(parts) >= 4 and parts[:2] == ["wday", "cxs"]:
            # Browser network logs expose Workday's API endpoint as
            # /wday/cxs/{tenant}/{site}/jobs. The public board lives at
            # /{site} on the same host.
            path = f"/{parts[3]}"
        elif ats_type == "workday" and parts:
            board_index = 1 if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) and len(parts) > 1 else 0
            path = "/" + "/".join(parts[:board_index + 1])
        elif (
            ats_type == "greenhouse"
            and len(parts) >= 3
            and parts[:2] == ["v1", "boards"]
        ):
            path = f"/{parts[2]}"
            parsed = parsed._replace(netloc="job-boards.greenhouse.io")
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
        decoded = re.sub(r"\\+/", "/", html.unescape(page_html))
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
            if ats_type == "greenhouse" and not self._greenhouse_candidate_is_usable(src):
                continue
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
