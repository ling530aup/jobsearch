"""SAP SuccessFactors public career-site adapter."""

import html
import re
from typing import List, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from .base import CareerFetcher
from ..models import Job


class SuccessFactorsFetcher(CareerFetcher):
    """Fetch public jobs from a SuccessFactors external career site.

    SuccessFactors' supported OData APIs require tenant credentials.  Public
    career sites instead expose an unauthenticated browser session and load
    their search results through the site's JavaScript.  This adapter uses the
    public site only; it does not require or attempt to use tenant credentials.
    """

    MAX_PAGES = 100

    def fetch_job_list(self) -> List[Job]:
        """Fetch all visible result pages from the public career site."""
        entry_url = self._resolve_entry_url()
        if not entry_url:
            return []

        # Legacy sites may return only the first result page in server HTML.
        # Also run the browser paginator and merge both sources instead of
        # treating a partial first page as a complete success.
        jobs = self._fetch_from_html(entry_url)
        seen_urls = {job.canonical_url for job in jobs}
        for job in self._fetch_with_playwright(entry_url):
            if job.canonical_url not in seen_urls:
                seen_urls.add(job.canonical_url)
                jobs.append(job)
        return jobs

    def _resolve_entry_url(self) -> Optional[str]:
        """Return a SuccessFactors URL from a direct or embedded career URL."""
        if self._is_successfactors_url(self.career_url):
            return self.career_url

        try:
            response = self.client.get(self.career_url)
            if response.status_code != 200:
                return None
        except Exception:
            return None

        pattern = r'''href=["']([^"']*successfactors\.(?:com|eu)[^"']*)["']'''
        match = re.search(pattern, response.text, re.IGNORECASE)
        if not match:
            return None
        return urljoin(self.career_url, html.unescape(match.group(1)))

    @staticmethod
    def _is_successfactors_url(url: str) -> bool:
        return bool(re.search(r"\.successfactors\.(?:com|eu)(?:/|$)", url, re.I))

    def _fetch_from_html(self, entry_url: str) -> List[Job]:
        """Try legacy sites that render their result links in server HTML."""
        try:
            # The initial request establishes the public career-site session.
            self.client.get(entry_url)
            response = self.client.get(self._job_search_url(entry_url))
            if response.status_code == 200:
                return self._parse_jobs(response.text, str(response.url))
        except Exception:
            pass
        return []

    def _job_search_url(self, entry_url: str) -> str:
        """Build the standard external SuccessFactors job-search page URL."""
        parsed = urlparse(entry_url)
        query = parse_qs(parsed.query)
        company = query.get("company", [""])[0]
        if not company:
            return entry_url

        params = {
            "company": company,
            "career_company": company,
            "navBarLevel": "JOB_SEARCH",
        }
        language = query.get("lang", [""])[0]
        if language:
            params["lang"] = language
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            "/portalcareer",
            "",
            urlencode(params),
            "",
        ))

    def _fetch_with_playwright(self, entry_url: str) -> List[Job]:
        """Render JavaScript-driven SuccessFactors result pages and paginate."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        jobs: List[Job] = []
        seen_urls = set()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                try:
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    page.goto(
                        self._job_search_url(entry_url),
                        wait_until="domcontentloaded",
                        timeout=self.timeout * 1000,
                    )
                    page.wait_for_timeout(2_000)

                    for _ in range(self.MAX_PAGES):
                        new_jobs = self._parse_jobs(page.content(), page.url)
                        for job in new_jobs:
                            if job.canonical_url not in seen_urls:
                                seen_urls.add(job.canonical_url)
                                jobs.append(job)

                        next_page = page.get_by_role("link", name=re.compile(r"^next$", re.I))
                        if next_page.count() == 0:
                            next_page = page.get_by_role("button", name=re.compile(r"^next$", re.I))
                        if next_page.count() == 0 or not next_page.first.is_enabled():
                            break

                        previous_url = page.url
                        next_page.first.click()
                        page.wait_for_timeout(1_000)
                        if page.url == previous_url and not self._parse_jobs(page.content(), page.url):
                            break
                finally:
                    browser.close()
        except Exception:
            return jobs
        return jobs

    def _parse_jobs(self, page_html: str, base_url: str) -> List[Job]:
        """Parse SuccessFactors job-detail links from a result page."""
        jobs = []
        seen_urls = set()
        link_pattern = r'''<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>'''

        for href, link_text in re.findall(link_pattern, page_html, re.DOTALL | re.I):
            url = urljoin(base_url, html.unescape(href))
            if not re.search(r"(?:jobreqid|jobid|job_req_id|jobpipeline)", url, re.I):
                continue

            title = re.sub(r"<[^>]+>", "", html.unescape(link_text))
            title = " ".join(title.split())
            if len(title) < 3:
                continue

            job = Job(
                company=self.company_name,
                title=title,
                url=url,
                source="successfactors",
            )
            if job.canonical_url in seen_urls:
                continue
            seen_urls.add(job.canonical_url)
            jobs.append(job)
        return jobs
