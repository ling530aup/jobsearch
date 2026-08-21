"""Greenhouse ATS adapter."""

import html
import re
from typing import List
from urllib.parse import urljoin, urlparse

from .base import CareerFetcher
from ..models import Job


class GreenhouseFetcher(CareerFetcher):
    """Fetcher for Greenhouse ATS job boards."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self.board_tokens = self._extract_board_tokens()
        self.board_token = self.board_tokens[0] if self.board_tokens else ""

    @staticmethod
    def _clean_board_tokens(tokens: List[str]) -> List[str]:
        """Keep only real Greenhouse board slugs, not embed/API placeholders."""
        cleaned = []
        for token in tokens:
            token = html.unescape(str(token)).replace(r"\/", "/").strip()
            token = token.split("/", 1)[0]
            if (
                token
                and token.casefold() not in {"embed", "v1", "boards"}
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", token)
                and token not in cleaned
            ):
                cleaned.append(token)
        return cleaned

    def _extract_board_tokens(self) -> List[str]:
        """Extract one or more Greenhouse boards from a direct or aggregate page."""
        # Pattern: boards.greenhouse.io/companyname
        parsed = urlparse(self.career_url)

        if "greenhouse.io" in parsed.netloc:
            # Do not turn a Greenhouse CDN resource such as ``/locales``
            # into a made-up board slug.  Browser network logs contain those
            # assets on otherwise unrelated career sites.
            from .detector import ATSDetector
            if not ATSDetector.is_public_board_url(self.career_url, "greenhouse"):
                return []
            # boards-api uses /v1/boards/{token}/jobs; public boards use
            # /{token}. Never mistake the API's "v1" segment for a token.
            path_parts = [
                re.sub(r"\\+", "", part)
                for part in parsed.path.strip("/").split("/")
                if part
            ]
            if parsed.netloc.casefold().startswith("boards-api."):
                if len(path_parts) >= 3 and path_parts[:2] == ["v1", "boards"]:
                    return [path_parts[2]]
            elif path_parts and path_parts[0] not in {"embed", "v1"}:
                return [path_parts[0]]

        try:
            response = self.client.get(self.career_url)
            decoded = html.unescape(response.text).replace(r"\/", "/")
            matches = re.findall(
                r'''(?:boards|job-boards)\.greenhouse\.(?:io|eu)/([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            )
            matches.extend(re.findall(
                r'''boards-api\.greenhouse\.io/v1/boards/([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            ))
            matches.extend(re.findall(
                r'''[?&]for=([^&#"']+)''', decoded, re.IGNORECASE
            ))
            tokens = self._clean_board_tokens(matches)
            if tokens:
                return tokens
        except Exception:
            pass

        # Try to find embedded token in page
        # Fallback to company name slug
        return [re.sub(r"[^a-z0-9]", "", self.company_name.casefold())]

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs from Greenhouse API."""
        jobs = []
        seen_urls = set()
        for board_token in self.board_tokens:
            if not self._clean_board_tokens([board_token]):
                continue
            api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
            board_jobs = []
            try:
                response = self.client.get(api_url)
                if response.status_code == 200:
                    data = response.json()
                    board_jobs = [
                        job for job_data in data.get("jobs", [])
                        if (job := self._parse_job(job_data))
                    ]
                    expected_total = self._api_total(data)
                    if expected_total and len(board_jobs) < expected_total:
                        # Some boards expose a lightweight response first and
                        # the complete posting set only when content is
                        # requested. Try the documented public API variant
                        # before falling back to rendered HTML.
                        try:
                            complete = self.client.get(f"{api_url}?content=true")
                            if complete.status_code == 200:
                                complete_data = complete.json()
                                board_jobs = self._merge_jobs(
                                    board_jobs,
                                    [
                                        job for job_data in complete_data.get("jobs", [])
                                        if (job := self._parse_job(job_data))
                                    ],
                                )
                        except Exception:
                            pass
                        if len(board_jobs) < expected_total:
                            board_jobs = self._merge_jobs(
                                board_jobs,
                                self._fetch_from_embed_api(board_token),
                            )
                else:
                    board_jobs = self._fetch_from_embed_api(board_token)
            except Exception as e:
                print(f"Error fetching Greenhouse jobs for {self.company_name}: {e}")
            for job in board_jobs:
                if job.canonical_url not in seen_urls:
                    seen_urls.add(job.canonical_url)
                    jobs.append(job)
        return jobs

    @staticmethod
    def _api_total(data: object) -> int:
        """Read Greenhouse's optional catalogue total without assuming it."""
        if not isinstance(data, dict):
            return 0
        meta = data.get("meta")
        if not isinstance(meta, dict):
            return 0
        for key in ("total", "total_count"):
            try:
                return max(0, int(meta.get(key, 0)))
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _merge_jobs(existing: List[Job], additions: List[Job]) -> List[Job]:
        """Merge fallback listings while preserving stable API ordering."""
        merged = list(existing)
        seen = {job.canonical_url for job in merged}
        for job in additions:
            if job.canonical_url not in seen:
                seen.add(job.canonical_url)
                merged.append(job)
        return merged

    def _fetch_from_embed_api(self, board_token: str = "") -> List[Job]:
        """Try fetching from embed API format."""
        jobs = []
        board_token = board_token or self.board_token
        board_tokens = self._clean_board_tokens([board_token])
        if not board_tokens:
            return jobs
        board_token = board_tokens[0]
        embed_url = f"https://boards.greenhouse.io/embed/job_board?for={board_token}"

        # A public board may redirect to a company-owned Greenhouse domain.
        # Try both surfaces when recovering from an incomplete API response.
        for page_url in (embed_url, f"https://boards.greenhouse.io/{board_token}"):
            try:
                response = self.client.get(page_url)
                if response.status_code == 200:
                    jobs = self._merge_jobs(
                        jobs,
                        self._parse_html_jobs(
                            response.text,
                            str(getattr(response, "url", page_url)),
                        ),
                    )
            except Exception:
                continue

        return jobs

    def _parse_job(self, job_data: dict) -> Job:
        """Parse job data from API response."""
        location = job_data.get("location", {})
        if isinstance(location, dict):
            location_str = location.get("name", "")
        elif isinstance(location, list):
            location_str = "; ".join(
                str(item.get("name", "") if isinstance(item, dict) else item).strip()
                for item in location
                if str(item.get("name", "") if isinstance(item, dict) else item).strip()
            )
        else:
            location_str = str(location)

        return Job(
            company=self.company_name,
            title=job_data.get("title", "Unknown"),
            url=job_data.get("absolute_url", ""),
            location=location_str,
            team=self._extract_department(job_data),
            source="greenhouse",
        )

    def _extract_department(self, job_data: dict) -> str:
        """Extract department/team from job data."""
        departments = job_data.get("departments", [])
        if departments and isinstance(departments, list):
            return departments[0].get("name", "")
        return ""

    def _parse_html_jobs(self, html_text: str, base_url: str = "") -> List[Job]:
        """Parse links from Greenhouse and white-label board HTML."""
        jobs = []
        pattern = r'<a\b([^>]*)>(.*?)</a\s*>'
        matches = re.findall(pattern, html_text, re.IGNORECASE | re.DOTALL)

        for attributes, raw_title in matches:
            href_match = re.search(
                r'\bhref\s*=\s*["\']([^"\']+)["\']',
                attributes,
                re.IGNORECASE,
            )
            if not href_match:
                continue
            url = urljoin(base_url, html.unescape(href_match.group(1)))
            parsed = urlparse(url)
            if "/jobs/" in parsed.path.casefold() or "gh_jid" in parsed.query.casefold():
                title = re.sub(r"<[^>]+>", " ", raw_title)
                title = re.sub(r"\s+", " ", html.unescape(title)).strip()
                if not title:
                    continue
                jobs.append(Job(
                    company=self.company_name,
                    title=title,
                    url=url,
                    source="greenhouse",
                ))

        return jobs
