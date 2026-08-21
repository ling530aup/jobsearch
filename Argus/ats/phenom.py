"""Public Phenom career-site adapter."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from .base import CareerFetcher
from ..models import Job


class PhenomFetcher(CareerFetcher):
    """Read server-rendered Phenom search JSON without browser pagination."""

    PAGE_SIZE = 100
    MAX_PAGES = 100
    PAGE_WORKERS = 5

    def fetch_job_list(self) -> List[Job]:
        """Fetch the public Phenom result feed in larger server-rendered pages."""
        search_url = self._search_url()
        jobs = []
        seen_urls = set()

        def fetch_page(offset: int) -> List[Job]:
            if self.stop_requested():
                return []
            page_url = self._page_url(search_url, offset)
            try:
                response = self.client.get(page_url)
            except Exception:
                return []
            if response.status_code != 200:
                return []
            return self._parse_jobs(response.text, str(response.url))

        first_page = fetch_page(0)
        if not first_page:
            return []
        page_size = len(first_page)

        def add_new(page_jobs: List[Job]) -> int:
            new_jobs = 0
            for job in page_jobs:
                if job.canonical_url not in seen_urls:
                    seen_urls.add(job.canonical_url)
                    jobs.append(job)
                    new_jobs += 1
            return new_jobs

        add_new(first_page)
        next_offset = page_size
        max_positions = self.PAGE_SIZE * self.MAX_PAGES
        while next_offset < max_positions and not self.stop_requested():
            offsets = list(range(
                next_offset,
                min(max_positions, next_offset + page_size * self.PAGE_WORKERS),
                page_size,
            ))
            with ThreadPoolExecutor(
                max_workers=min(self.PAGE_WORKERS, len(offsets)),
                thread_name_prefix="phenom-page",
            ) as executor:
                pages = list(executor.map(fetch_page, offsets))
            reached_end = False
            for page_jobs in pages:
                if not page_jobs or add_new(page_jobs) == 0 or len(page_jobs) < page_size:
                    reached_end = True
                    break
            if reached_end:
                break
            next_offset += page_size * len(offsets)
        return jobs

    def _search_url(self) -> str:
        parsed = urlparse(self.career_url)
        if "/search-results" in parsed.path.casefold():
            return self.career_url
        path = parsed.path.rstrip("/")
        # Phenom landing pages commonly end in ``/home`` while the catalogue
        # is the sibling ``/search-results`` route, not a child of ``home``.
        if path.casefold().endswith("/home"):
            path = path.rsplit("/", 1)[0]
        path += "/search-results"
        return urlunparse(parsed._replace(path=path, query="", fragment=""))

    @staticmethod
    def _page_url(search_url: str, offset: int) -> str:
        parsed = urlparse(search_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"from": str(offset), "size": str(PhenomFetcher.PAGE_SIZE)})
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _parse_jobs(self, page_html: str, page_url: str) -> List[Job]:
        decoder = json.JSONDecoder()
        for match in re.finditer(r'"jobs"\s*:\s*\[', page_html):
            try:
                records, _ = decoder.raw_decode(page_html[match.end() - 1:])
            except (TypeError, ValueError):
                continue
            if not isinstance(records, list) or not records:
                continue
            if not isinstance(records[0], dict) or not records[0].get("jobId"):
                continue
            return self._jobs_from_records(records, page_url)
        return []

    def _jobs_from_records(self, records: list, page_url: str) -> List[Job]:
        jobs = []
        parsed = urlparse(page_url)
        base_path = parsed.path.split("/search-results", 1)[0].rstrip("/")
        for record in records:
            if not isinstance(record, dict) or not record.get("title"):
                continue
            job_id = str(record.get("jobId") or record.get("reqId") or "")
            if not job_id:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", str(record["title"]).casefold()).strip("-")
            detail_path = f"{base_path}/job/{quote(job_id)}/{quote(slug)}"
            detail_url = urlunparse(parsed._replace(
                path=detail_path,
                query="",
                fragment="",
            ))
            jobs.append(Job(
                company=self.company_name,
                title=str(record["title"]),
                url=detail_url,
                location=str(record.get("location") or ""),
                team=str(record.get("category") or record.get("unit") or ""),
                source="phenom",
            ))
        return jobs
