"""TalentBrew/Radancy server-rendered job search adapter."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import math
import re
from typing import List
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .generic import GenericFetcher
from ..models import Job


class TalentBrewFetcher(GenericFetcher):
    """Fetch all TalentBrew pages using their public search result HTML."""

    PAGE_WORKERS = 3
    MAX_PAGES = 1_000

    def fetch_job_list(self) -> List[Job]:
        search_url = self._resolve_search_url()
        if not search_url:
            return super().fetch_job_list()
        try:
            response = self.client.get(search_url)
            if response.status_code != 200:
                return super().fetch_job_list()
        except Exception:
            return super().fetch_job_list()

        first_html = response.text
        first_url = str(response.url)
        jobs = self._extract_jobs_from_html(first_html, first_url)
        total = self._meta_int(first_html, "search-analytics-total-jobs")
        page_size = self._analytics_page_size(first_html)
        if not total or not page_size:
            # Its normal Next links still work with the generic paginator.
            self.career_url = first_url
            return super().fetch_job_list()

        page_count = min(self.MAX_PAGES, math.ceil(total / page_size))
        if page_count <= 1:
            return self._job_detail_links_only(jobs, first_url)

        def fetch_page(page_number: int) -> List[Job]:
            page_url = self._page_url(first_url, page_number)
            try:
                page_response = self.client.get(page_url)
                if page_response.status_code == 200:
                    return self._extract_jobs_from_html(
                        page_response.text,
                        str(page_response.url),
                    )
            except Exception:
                pass
            return []

        with ThreadPoolExecutor(
            max_workers=self.PAGE_WORKERS,
            thread_name_prefix="talentbrew-page",
        ) as executor:
            futures = [executor.submit(fetch_page, page) for page in range(2, page_count + 1)]
            for future in as_completed(futures):
                jobs.extend(future.result())

        detail_jobs = self._job_detail_links_only(jobs, first_url)
        unique = {}
        for job in detail_jobs:
            unique.setdefault(job.canonical_url, job)
        return list(unique.values())

    def _resolve_search_url(self) -> str:
        if "search-jobs" in urlparse(self.career_url).path.casefold():
            return self.career_url
        try:
            response = self.client.get(self.career_url)
            if response.status_code != 200:
                return ""
        except Exception:
            return ""
        match = re.search(
            r'''(?:href|action)=["']([^"']*search-jobs[^"']*)["']''',
            response.text,
            re.IGNORECASE,
        )
        return urljoin(str(response.url), html.unescape(match.group(1))) if match else ""

    @staticmethod
    def _meta_int(page_html: str, name: str) -> int:
        match = re.search(
            rf'''<meta[^>]+name=["']{re.escape(name)}["'][^>]+content=["'](\d+)["']''',
            page_html,
            re.IGNORECASE,
        )
        return int(match.group(1)) if match else 0

    @staticmethod
    def _analytics_page_size(page_html: str) -> int:
        match = re.search(
            r'''<meta[^>]+name=["']search-analytics-jobIds["'][^>]+content=["']([^"']*)["']''',
            page_html,
            re.IGNORECASE,
        )
        if not match:
            return 0
        try:
            value = json.loads(html.unescape(match.group(1)))
            return len(value) if isinstance(value, dict) else 0
        except (json.JSONDecodeError, TypeError):
            return 0

    @staticmethod
    def _page_url(url: str, page_number: int) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["p"] = str(page_number)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _job_detail_links_only(jobs: List[Job], search_url: str) -> List[Job]:
        search_host = urlparse(search_url).netloc.casefold()
        return [
            job for job in jobs
            if urlparse(job.url).netloc.casefold() == search_host
            and bool(re.search(r"/job/[^/]+/.+?/\d+", urlparse(job.url).path, re.I))
        ]
