"""Personio public XML job-feed adapter."""

from typing import List
from urllib.parse import urlparse
from xml.etree import ElementTree

from .base import CareerFetcher
from ..models import Job


class PersonioFetcher(CareerFetcher):
    """Fetch all jobs from a public Personio XML feed."""

    def fetch_job_list(self) -> List[Job]:
        parsed = urlparse(self.career_url)
        if not parsed.netloc.casefold().endswith((".jobs.personio.de", ".jobs.personio.com")):
            return []
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        try:
            response = self.client.get(f"{origin}/xml", params={"language": "en"})
            if response.status_code != 200:
                return []
            root = ElementTree.fromstring(response.content)
        except Exception:
            return []

        jobs = []
        for position in root.findall(".//position"):
            job_id = self._text(position, "id")
            title = self._text(position, "name")
            if not job_id or not title:
                continue
            jobs.append(Job(
                company=self.company_name,
                title=title,
                url=f"{origin}/job/{job_id}?language=en",
                location=self._text(position, "office"),
                team=self._text(position, "department") or self._text(position, "recruitingCategory"),
                source="personio",
            ))
        return jobs

    @staticmethod
    def _text(element, name: str) -> str:
        child = element.find(name)
        return (child.text or "").strip() if child is not None else ""
