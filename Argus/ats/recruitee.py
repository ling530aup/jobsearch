"""Recruitee public careers API adapter."""

from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class RecruiteeFetcher(CareerFetcher):
    """Fetch every published offer from a Recruitee careers site."""

    def fetch_job_list(self) -> List[Job]:
        parsed = urlparse(self.career_url)
        if not parsed.netloc.casefold().endswith(".recruitee.com"):
            return []
        api_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/api/offers/"
        try:
            response = self.client.get(api_url)
            if response.status_code != 200:
                return []
            offers = response.json().get("offers") or []
        except Exception:
            return []

        jobs = []
        for offer in offers if isinstance(offers, list) else []:
            title = offer.get("title")
            url = offer.get("careers_url")
            if not title or not url:
                continue
            location = offer.get("location") or ""
            if offer.get("remote") and "remote" not in location.casefold():
                location = f"{location}; Remote" if location else "Remote"
            elif offer.get("hybrid") and "hybrid" not in location.casefold():
                location = f"{location}; Hybrid" if location else "Hybrid"
            jobs.append(Job(
                company=self.company_name,
                title=str(title),
                url=str(url),
                location=str(location),
                team=str(offer.get("department") or offer.get("category_code") or ""),
                source="recruitee",
            ))
        return jobs
