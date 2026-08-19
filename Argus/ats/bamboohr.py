"""BambooHR public careers endpoint adapter."""

from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class BambooHRFetcher(CareerFetcher):
    """Fetch all currently published BambooHR job openings."""

    def fetch_job_list(self) -> List[Job]:
        parsed = urlparse(self.career_url)
        if not parsed.netloc.casefold().endswith(".bamboohr.com"):
            return []
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        try:
            response = self.client.get(f"{origin}/careers/list")
            if response.status_code != 200:
                return []
            data = response.json()
            openings = data.get("result") or []
        except Exception:
            return []

        jobs = []
        for opening in openings if isinstance(openings, list) else []:
            job_id = opening.get("id")
            title = opening.get("jobOpeningName") or opening.get("title")
            if not job_id or not title:
                continue
            location_data = opening.get("location") or {}
            location = (
                location_data.get("locationName", "")
                if isinstance(location_data, dict)
                else str(location_data)
            )
            jobs.append(Job(
                company=self.company_name,
                title=str(title),
                url=f"{origin}/careers/{job_id}",
                location=location,
                team=str(opening.get("departmentLabel") or opening.get("department") or ""),
                source="bamboohr",
            ))
        return jobs
