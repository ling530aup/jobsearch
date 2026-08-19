"""Apple public server-rendered job search adapter."""

from concurrent.futures import ThreadPoolExecutor
from html import unescape
import re
from typing import List
from urllib.parse import urljoin, urlparse

from .base import CareerFetcher
from ..models import Job


class AppleFetcher(CareerFetcher):
    """Fetch Apple search pages in bounded parallel batches."""

    PAGE_WORKERS = 4
    BATCH_SIZE = 4
    MAX_PAGES = 200

    def fetch_job_list(self) -> List[Job]:
        parsed = urlparse(self.career_url)
        locale_match = re.match(r"/([a-z]{2}-[a-z]{2})", parsed.path, re.I)
        locale = locale_match.group(1) if locale_match else "en-us"
        base_url = f"https://jobs.apple.com/{locale}/search"
        jobs = []
        seen_urls = set()

        def fetch_page(page_number: int) -> List[Job]:
            try:
                response = self.client.get(base_url, params={"page": page_number})
                if response.status_code == 200:
                    return self._parse_page(response.text, str(response.url))
            except Exception:
                pass
            return []

        with ThreadPoolExecutor(
            max_workers=self.PAGE_WORKERS,
            thread_name_prefix="apple-page",
        ) as executor:
            for batch_start in range(1, self.MAX_PAGES + 1, self.BATCH_SIZE):
                page_numbers = range(
                    batch_start,
                    min(self.MAX_PAGES + 1, batch_start + self.BATCH_SIZE),
                )
                page_results = list(executor.map(fetch_page, page_numbers))
                new_in_batch = 0
                for page_jobs in page_results:
                    for job in page_jobs:
                        if job.canonical_url not in seen_urls:
                            seen_urls.add(job.canonical_url)
                            jobs.append(job)
                            new_in_batch += 1
                if not new_in_batch or all(not page_jobs for page_jobs in page_results):
                    break
        return jobs

    def _parse_page(self, page_html: str, base_url: str) -> List[Job]:
        jobs = []
        seen_urls = set()
        blocks = re.findall(
            r'''<li[^>]+data-core-accordion-item[^>]*>(.*?)</li>''',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        for block in blocks:
            link = re.search(
                r'''<a[^>]+href=["']([^"']*/details/[^"']+)["'][^>]*>(.*?)</a>''',
                block,
                re.DOTALL | re.IGNORECASE,
            )
            if not link or "/locationpicker" in link.group(1).casefold():
                continue
            title = unescape(" ".join(re.sub(r"<[^>]+>", " ", link.group(2)).split()))
            location = self._span_text(block, r'''id=["']search-store-name''')
            team = self._span_text(block, r'''class=["'][^"']*team-name''')
            if not title:
                continue
            job = Job(
                company=self.company_name,
                title=title,
                url=urljoin(base_url, link.group(1)),
                location=location,
                team=team,
                source="apple",
            )
            if job.canonical_url not in seen_urls:
                seen_urls.add(job.canonical_url)
                jobs.append(job)
        return jobs

    @staticmethod
    def _span_text(block: str, attribute_pattern: str) -> str:
        match = re.search(
            rf'''<span[^>]+{attribute_pattern}[^>]*>(.*?)</span>''',
            block,
            re.DOTALL | re.IGNORECASE,
        )
        return unescape(
            " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())
        ) if match else ""
