"""Google Careers fetcher using server-rendered structured data."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import re
from typing import List, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
)
from ..models import Job


class GoogleFetcher(CareerFetcher):
    """Fetcher for Google Careers with an HTTP-first browser fallback."""

    BASE_URL = "https://www.google.com/about/careers/applications/jobs/results/"
    JOB_URL_TEMPLATE = "https://www.google.com/about/careers/applications/jobs/results/{job_id}"
    PAGE_WORKERS = 4
    MAX_PAGES = 250

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch pages concurrently from Google's server-rendered job data."""
        first_url = self._page_url(1)
        try:
            response = self.client.get(first_url)
            if response.status_code == 200:
                first_jobs, total, page_size = self._parse_structured_page(
                    response.text,
                    str(response.url),
                )
                if first_jobs and total and page_size:
                    return self._fetch_http_pages(first_jobs, total, page_size)
        except Exception:
            pass

        # Keep a compatibility path in case Google changes its embedded data.
        return self._fetch_with_playwright()

    def _fetch_http_pages(
        self,
        first_jobs: List[Job],
        total: int,
        page_size: int,
    ) -> List[Job]:
        """Fetch remaining result pages with bounded concurrency."""
        page_count = min(self.MAX_PAGES, math.ceil(total / page_size))
        if page_count <= 1:
            return first_jobs

        def fetch_page(page_number: int) -> Tuple[int, List[Job]]:
            try:
                response = self.client.get(self._page_url(page_number))
                if response.status_code != 200:
                    return page_number, []
                jobs, _total, _page_size = self._parse_structured_page(
                    response.text,
                    str(response.url),
                )
                return page_number, jobs
            except Exception:
                return page_number, []

        pages = {1: first_jobs}
        failed_pages = []
        with ThreadPoolExecutor(
            max_workers=self.PAGE_WORKERS,
            thread_name_prefix="google-page",
        ) as executor:
            futures = {
                executor.submit(fetch_page, page_number): page_number
                for page_number in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                page_number, page_jobs = future.result()
                if page_jobs:
                    pages[page_number] = page_jobs
                else:
                    failed_pages.append(page_number)

        if failed_pages:
            print(
                f"Google Careers skipped {len(failed_pages)} unavailable pages "
                f"out of {page_count}"
            )

        jobs = []
        seen_ids = set()
        for page_number in sorted(pages):
            for job in pages[page_number]:
                job_id = self._job_id(job.url)
                dedupe_key = job_id or job.canonical_url
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                jobs.append(job)
        return jobs

    def _page_url(self, page_number: int) -> str:
        """Preserve configured search parameters while changing the page."""
        source = self.career_url if "/jobs/results" in self.career_url else self.BASE_URL
        parsed = urlparse(source)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page_number)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _parse_structured_page(
        self,
        page_html: str,
        page_url: str,
    ) -> Tuple[List[Job], int, int]:
        """Parse the ds:1 payload rendered into every Google results page."""
        match = re.search(
            r'''<script[^>]+class=["']ds:1["'][^>]*>\s*'''
            r'''AF_initDataCallback\(\{.*?\bdata:(.*?),\s*sideChannel:''',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return [], 0, 0
        try:
            payload = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            return [], 0, 0
        if not isinstance(payload, list) or not payload:
            return [], 0, 0

        records = payload[0] if isinstance(payload[0], list) else []
        total = payload[2] if len(payload) > 2 and isinstance(payload[2], int) else 0
        page_size = payload[3] if len(payload) > 3 and isinstance(payload[3], int) else len(records)
        base_match = re.search(
            r'''<base[^>]+href=["']([^"']+)["']''',
            page_html,
            re.IGNORECASE,
        )
        link_base = base_match.group(1) if base_match else page_url
        detail_urls = {
            job_id: urljoin(link_base, href)
            for href, job_id in re.findall(
                r'''href=["']([^"']*jobs/results/(\d{15,})[^"']*)["']''',
                page_html,
                re.IGNORECASE,
            )
        }

        jobs = []
        for record in records:
            if not isinstance(record, list) or len(record) < 2:
                continue
            job_id = str(record[0] or "")
            title = str(record[1] or "").strip()
            if not job_id or not title:
                continue
            location_values = []
            raw_locations = (
                record[9]
                if len(record) > 9 and isinstance(record[9], list)
                else []
            )
            for location in raw_locations:
                if isinstance(location, list) and location and location[0]:
                    value = str(location[0]).strip()
                    if value and value not in location_values:
                        location_values.append(value)
            jobs.append(Job(
                company=self.company_name,
                title=title,
                url=detail_urls.get(job_id) or self.JOB_URL_TEMPLATE.format(job_id=job_id),
                location="; ".join(location_values),
                source="google",
            ))
        return jobs, total, page_size

    @staticmethod
    def _job_id(url: str) -> str:
        match = re.search(r"jobs/results/(\d{15,})", str(url))
        return match.group(1) if match else ""

    def _fetch_with_playwright(self) -> List[Job]:
        """Compatibility fallback when Google's embedded payload changes."""
        jobs = []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
            return jobs

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = create_browser_context(browser)
                page = context.new_page()
                install_browser_page_handlers(page)

                page_num = 1
                max_pages = self.MAX_PAGES
                seen_ids = set()

                while page_num <= max_pages:
                    if self.stop_requested():
                        break
                    url = f"{self.BASE_URL}?page={page_num}"

                    try:
                        goto_browser_page(page, url, self.timeout)
                        page.wait_for_timeout(2000)
                        dismiss_browser_overlays(page)
                    except Exception as e:
                        print(f"Error loading page {page_num}: {e}")
                        break

                    # Extract jobs using both HTML and text
                    html = page.content()
                    text = page.inner_text("body")
                    page_jobs = self._extract_jobs(html, text, seen_ids)

                    if not page_jobs:
                        # No more jobs found
                        break

                    jobs.extend(page_jobs)
                    page_num += 1

                    # Progress indicator every 10 pages
                    if page_num % 10 == 0:
                        print(f"    Fetched {len(jobs)} jobs so far...")

                context.close()
                browser.close()

        except Exception as e:
            print(f"Error fetching Google jobs: {e}")

        return jobs

    def _extract_jobs(self, html: str, text: str, seen_ids: set) -> List[Job]:
        """Extract job listings from page content."""
        jobs = []

        # Find job IDs from URLs
        job_id_pattern = r'jobs/results/(\d{15,})'
        job_ids = list(dict.fromkeys(re.findall(job_id_pattern, html)))  # Preserve order, remove dupes

        # Parse text to find job titles and locations
        # Text structure: Title\ncorporate_fare\nCompany\nplace\nLocation
        lines = [line.strip() for line in text.split('\n') if line.strip()]

        job_info = []  # List of (title, location) tuples
        i = 0
        while i < len(lines):
            line = lines[i]

            # Look for job title patterns (ends with role keywords or is followed by corporate_fare)
            if i + 4 < len(lines) and lines[i + 1] == "corporate_fare":
                title = line
                # Skip corporate_fare and company name
                # Find location after 'place'
                location = ""
                for j in range(i + 2, min(i + 10, len(lines))):
                    if lines[j] == "place" and j + 1 < len(lines):
                        location = lines[j + 1]
                        # Clean up location - remove semicolons and extra parts
                        location = location.split(';')[0].strip()
                        break

                if title and len(title) > 5:
                    job_info.append((title, location))

            i += 1

        # Match job info with job IDs
        for idx, job_id in enumerate(job_ids):
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            if idx < len(job_info):
                title, location = job_info[idx]
            else:
                title = f"Job {job_id}"
                location = ""

            jobs.append(Job(
                company=self.company_name,
                title=title,
                url=self.JOB_URL_TEMPLATE.format(job_id=job_id),
                location=location,
                source="google",
            ))

        return jobs
