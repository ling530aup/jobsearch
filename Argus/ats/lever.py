"""Lever ATS adapter."""

import html
import re
from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class LeverFetcher(CareerFetcher):
    """Fetcher for Lever ATS job boards."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self.company_slug = self._extract_company_slug()

    def _extract_company_slug(self) -> str:
        """Extract Lever company slug from URL."""
        parsed = urlparse(self.career_url)

        if "lever.co" in parsed.netloc:
            # Direct Lever URL: jobs.lever.co/companyname
            path_parts = parsed.path.strip("/").split("/")
            if path_parts:
                return path_parts[0]

        try:
            response = self.client.get(self.career_url)
            decoded = html.unescape(response.text).replace(r"\/", "/")
            match = re.search(
                r'''(?:jobs\.lever\.co/|api\.lever\.co/v0/postings/)([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
        except Exception:
            pass

        # Fallback to company name slug
        return re.sub(r"[^a-z0-9]", "", self.company_name.casefold())

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs from Lever."""
        jobs = []

        # Lever jobs endpoint (returns HTML by default, JSON with ?mode=json)
        api_url = f"https://api.lever.co/v0/postings/{self.company_slug}"

        try:
            response = self.client.get(api_url)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    for job_data in data:
                        job = self._parse_job(job_data)
                        if job:
                            jobs.append(job)
            else:
                # Try fetching from HTML page
                jobs = self._fetch_from_html()
        except Exception as e:
            print(f"Error fetching Lever jobs for {self.company_name}: {e}")
            jobs = self._fetch_from_html()

        return jobs

    def _fetch_from_html(self) -> List[Job]:
        """Fetch jobs by parsing the Lever HTML page."""
        jobs = []
        html_url = f"https://jobs.lever.co/{self.company_slug}"

        try:
            response = self.client.get(html_url)
            if response.status_code == 200:
                jobs = self._parse_html_jobs(response.text)
        except Exception:
            pass

        return jobs

    def _parse_job(self, job_data: dict) -> Job:
        """Parse job data from API response."""
        categories = job_data.get("categories", {})

        return Job(
            company=self.company_name,
            title=job_data.get("text", "Unknown"),
            url=job_data.get("hostedUrl", ""),
            location=categories.get("location", ""),
            team=categories.get("team", ""),
            source="lever",
        )

    def _parse_html_jobs(self, html: str) -> List[Job]:
        """Parse jobs from HTML content."""
        jobs = []

        # Pattern for Lever job postings
        # Looking for posting divs with links
        posting_pattern = r'<a[^>]+class="[^"]*posting-title[^"]*"[^>]+href="([^"]+)"[^>]*>.*?<h5[^>]*>([^<]+)</h5>'
        matches = re.findall(posting_pattern, html, re.DOTALL | re.IGNORECASE)

        if not matches:
            # Fallback pattern
            pattern = r'href="(https://jobs\.lever\.co/[^/]+/[^"]+)"[^>]*>([^<]+)</a>'
            matches = re.findall(pattern, html, re.IGNORECASE)

        for url, title in matches:
            jobs.append(Job(
                company=self.company_name,
                title=title.strip(),
                url=url,
                source="lever",
            ))

        return jobs
