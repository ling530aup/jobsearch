"""Greenhouse ATS adapter."""

import html
import re
from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class GreenhouseFetcher(CareerFetcher):
    """Fetcher for Greenhouse ATS job boards."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self.board_token = self._extract_board_token()

    def _extract_board_token(self) -> str:
        """Extract Greenhouse board token from URL."""
        # Pattern: boards.greenhouse.io/companyname
        parsed = urlparse(self.career_url)

        if "greenhouse.io" in parsed.netloc:
            # Direct Greenhouse URL
            path_parts = parsed.path.strip("/").split("/")
            if path_parts:
                return path_parts[0]

        try:
            response = self.client.get(self.career_url)
            decoded = html.unescape(response.text).replace(r"\/", "/")
            match = re.search(
                r'''(?:boards|job-boards)\.greenhouse\.(?:io|eu)/([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            ) or re.search(
                r'''boards-api\.greenhouse\.io/v1/boards/([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            ) or re.search(r'''[?&]for=([^&#"']+)''', decoded, re.IGNORECASE)
            if match:
                return match.group(1)
        except Exception:
            pass

        # Try to find embedded token in page
        # Fallback to company name slug
        return re.sub(r"[^a-z0-9]", "", self.company_name.casefold())

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs from Greenhouse API."""
        jobs = []

        # Greenhouse public API endpoint
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs"

        try:
            response = self.client.get(api_url)
            if response.status_code == 200:
                data = response.json()
                for job_data in data.get("jobs", []):
                    job = self._parse_job(job_data)
                    if job:
                        jobs.append(job)
            else:
                # Try alternate API format
                jobs = self._fetch_from_embed_api()
        except Exception as e:
            print(f"Error fetching Greenhouse jobs for {self.company_name}: {e}")

        return jobs

    def _fetch_from_embed_api(self) -> List[Job]:
        """Try fetching from embed API format."""
        jobs = []
        embed_url = f"https://boards.greenhouse.io/embed/job_board?for={self.board_token}"

        try:
            response = self.client.get(embed_url)
            if response.status_code == 200:
                # Parse HTML for job listings
                jobs = self._parse_html_jobs(response.text)
        except Exception:
            pass

        return jobs

    def _parse_job(self, job_data: dict) -> Job:
        """Parse job data from API response."""
        location = job_data.get("location", {})
        location_str = location.get("name", "") if isinstance(location, dict) else str(location)

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

    def _parse_html_jobs(self, html: str) -> List[Job]:
        """Parse jobs from HTML content."""
        jobs = []
        # Simple regex pattern for Greenhouse job listings
        pattern = r'<a[^>]+href="([^"]*greenhouse[^"]*)"[^>]*>([^<]+)</a>'
        matches = re.findall(pattern, html, re.IGNORECASE)

        for url, title in matches:
            if "/jobs/" in url.lower():
                jobs.append(Job(
                    company=self.company_name,
                    title=title.strip(),
                    url=url,
                    source="greenhouse",
                ))

        return jobs
