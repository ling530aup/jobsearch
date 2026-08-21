"""SmartRecruiters public postings adapter."""

from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class SmartRecruitersFetcher(CareerFetcher):
    """Fetch all postings from SmartRecruiters' unauthenticated public API."""

    PAGE_SIZE = 100
    MAX_POSTINGS = 10_000

    def fetch_job_list(self) -> List[Job]:
        company_id = self._extract_company_id()
        if not company_id:
            return []
        api_url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
        jobs = []
        seen_ids = set()
        offset = 0

        while offset < self.MAX_POSTINGS:
            try:
                response = self.client.get(
                    api_url,
                    params={"limit": self.PAGE_SIZE, "offset": offset},
                )
                if response.status_code != 200:
                    break
                data = response.json()
            except Exception:
                break
            postings = data.get("content", [])
            if not isinstance(postings, list) or not postings:
                break

            new_ids = 0
            for item in postings:
                posting_id = item.get("id")
                title = item.get("name")
                if not posting_id or posting_id in seen_ids or not title:
                    continue
                seen_ids.add(posting_id)
                new_ids += 1
                location = item.get("location") or {}
                location_name = location.get("fullLocation") if isinstance(location, dict) else ""
                if isinstance(location, dict) and location.get("remote"):
                    location_name = f"{location_name}; Remote" if location_name else "Remote"
                department = item.get("department") or {}
                team = department.get("label", "") if isinstance(department, dict) else ""
                jobs.append(Job(
                    company=self.company_name,
                    title=title,
                    url=f"https://jobs.smartrecruiters.com/{company_id}/{posting_id}",
                    location=location_name or "",
                    team=team,
                    source="smartrecruiters",
                ))

            offset += len(postings)
            total = data.get("totalFound")
            if new_ids == 0 or len(postings) < self.PAGE_SIZE or (
                isinstance(total, int) and offset >= total
            ):
                break
        return jobs

    def _extract_company_id(self) -> str:
        parsed = urlparse(self.career_url)
        host = parsed.netloc.casefold()
        parts = [part for part in parsed.path.split("/") if part]
        if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"} and parts:
            return parts[0]
        if host == "api.smartrecruiters.com":
            try:
                index = parts.index("companies")
                return parts[index + 1]
            except (ValueError, IndexError):
                return ""
        return ""
