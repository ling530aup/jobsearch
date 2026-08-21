"""Salesforce public careers feed adapter."""

import json
from typing import List

from .base import CareerFetcher
from .generic import GenericFetcher
from ..models import Job


class SalesforceFetcher(CareerFetcher):
    """Fetch Salesforce's complete public catalogue from its static feed.

    The careers page is a client-side shell.  Its public careers bundle loads
    the full catalogue from the same stable JSON feed, so scraping the shell
    or clicking a finite number of rendered cards silently loses most jobs.
    """

    # Salesforce has changed the static bundle path more than once. Keep the
    # feed as a fast path, but do not make one stale asset URL the single point
    # of failure; the public jobs page below is the authoritative fallback.
    FEED_URLS = (
        "https://a.sfdcstatic.com/digital/xsf/careers/"
        "SFDC:us:company:careers:jobs/jobs_2.json",
        "https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_2.json",
    )

    def fetch_job_list(self) -> List[Job]:
        jobs = []
        seen_urls = set()
        for feed_url in self.FEED_URLS:
            try:
                response = self.client.get(feed_url)
                if response.status_code != 200:
                    continue
                payload = response.json()
            except (Exception, ValueError):
                continue
            records = payload.get("Report_Entry", []) if isinstance(payload, dict) else []
            if not isinstance(records, list):
                continue
            jobs.extend(self._parse_records(records, seen_urls))
            if jobs:
                return jobs

        # The old careers shell may redirect to the current jobs catalogue.
        # Reuse GenericFetcher's bounded static pagination so a changed
        # Salesforce bundle does not turn the result into zero jobs.
        for page_url in self._catalogue_urls():
            generic = GenericFetcher(self.company_name, page_url, self.timeout)
            generic._client = self.client
            for job in generic._fetch_simple():
                if job.canonical_url not in seen_urls:
                    seen_urls.add(job.canonical_url)
                    job.source = "salesforce"
                    jobs.append(job)
            if jobs:
                break
        return jobs

    def _catalogue_urls(self) -> tuple:
        return (
            self.career_url,
            "https://www.salesforce.com/company/careers/jobs/",
            "https://careers.salesforce.com/en/jobs/",
        )

    def _parse_records(self, records: list, seen_urls: set) -> List[Job]:
        jobs = []
        for record in records:
            if not isinstance(record, dict):
                continue
            title = str(record.get("Job_Posting_Title") or "").strip()
            url = str(record.get("External_Job_Posting_Site") or "").strip()
            if not title or not url:
                continue
            job = Job(
                company=self.company_name,
                title=title,
                url=url,
                location=self._location(record),
                source="salesforce",
            )
            if job.canonical_url in seen_urls:
                continue
            seen_urls.add(job.canonical_url)
            jobs.append(job)
        return jobs

    @staticmethod
    def _location(record: dict) -> str:
        locations = record.get("Locations")
        if isinstance(locations, list):
            values = [str(item).strip() for item in locations if str(item).strip()]
            if values:
                return ", ".join(dict.fromkeys(values))
        return str(record.get("Job_Requisition_Primary_Location") or "").strip()
