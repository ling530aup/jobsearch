"""Google Careers fetcher using Playwright."""

import re
from typing import List, Tuple

from .base import CareerFetcher, dismiss_browser_overlays, install_browser_page_handlers
from ..models import Job


class GoogleFetcher(CareerFetcher):
    """Fetcher for Google careers using Playwright pagination."""

    BASE_URL = "https://www.google.com/about/careers/applications/jobs/results/"
    JOB_URL_TEMPLATE = "https://www.google.com/about/careers/applications/jobs/results/{job_id}"

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from Google careers by paginating through results."""
        jobs = []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
            return jobs

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                install_browser_page_handlers(page)

                page_num = 1
                max_pages = 200  # Safety limit
                seen_ids = set()

                while page_num <= max_pages:
                    url = f"{self.BASE_URL}?page={page_num}"

                    try:
                        page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
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
