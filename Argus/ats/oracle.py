"""Oracle Recruiting Candidate Experience public job-board adapter."""

import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from .base import CareerFetcher
from ..models import Job


class OracleFetcher(CareerFetcher):
    """Fetch all public requisitions from Oracle Candidate Experience."""

    # Oracle accepts 100 on Candidate Experience sites; this cuts requests by
    # 75% compared with the UI's usual 25-result page size.
    PAGE_SIZE = 100
    MAX_POSTINGS = 10_000

    def fetch_job_list(self) -> List[Job]:
        board = self._resolve_board()
        if not board:
            return []
        board_url, site_number = board
        parsed = urlparse(board_url)
        api_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            "/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            "",
            "",
            "",
        ))

        jobs: List[Job] = []
        seen_ids = set()
        offset = 0
        total: Optional[int] = None
        while offset < self.MAX_POSTINGS:
            finder = (
                f"findReqs;siteNumber={site_number},"
                f"limit={self.PAGE_SIZE},offset={offset}"
            )
            try:
                response = self.client.get(
                    api_url,
                    params={
                        "onlyData": "true",
                        "expand": "requisitionList",
                        "finder": finder,
                    },
                    headers={"REST-Framework-Version": "4"},
                )
                if response.status_code != 200:
                    break
                data = response.json()
            except Exception:
                break

            search_items = data.get("items") or []
            if not search_items or not isinstance(search_items[0], dict):
                break
            search = search_items[0]
            requisition_list = search.get("requisitionList") or []
            postings = (
                requisition_list.get("items") or []
                if isinstance(requisition_list, dict)
                else requisition_list
            )
            if not isinstance(postings, list) or not postings:
                break
            if total is None and isinstance(search.get("TotalJobsCount"), int):
                total = search["TotalJobsCount"]

            new_ids = 0
            for item in postings:
                job_id = str(item.get("Id") or item.get("RequisitionId") or "")
                title = item.get("Title") or item.get("JobTitle")
                if not job_id or not title or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                new_ids += 1
                location = item.get("PrimaryLocation") or ""
                workplace = item.get("WorkplaceType") or ""
                if workplace and workplace.casefold() not in str(location).casefold():
                    location = f"{location}; {workplace}" if location else workplace
                jobs.append(Job(
                    company=self.company_name,
                    title=str(title),
                    url=f"{board_url.rstrip('/')}/job/{job_id}",
                    location=str(location),
                    team=str(item.get("JobFunction") or item.get("JobFamily") or ""),
                    source="oracle",
                ))

            offset += len(postings)
            if new_ids == 0 or len(postings) < self.PAGE_SIZE:
                break
            if total is not None and offset >= total:
                break
        return jobs

    def _resolve_board(self) -> Optional[Tuple[str, str]]:
        """Return a clean public board URL and its internal site number."""
        try:
            response = self.client.get(self.career_url)
            if response.status_code != 200:
                return None
        except Exception:
            return None

        final_url = str(response.url)
        parsed = urlparse(final_url)
        match = re.search(
            r"/hcmUI/CandidateExperience/([^/]+)/sites/([^/?#]+)",
            parsed.path,
            re.IGNORECASE,
        )
        if not match:
            return None
        language, site_slug = match.groups()
        site_match = re.search(
            r'''data-sitenumber\s*=\s*["']([^"']+)["']''',
            response.text,
            re.IGNORECASE,
        )
        site_number = site_match.group(1) if site_match else site_slug
        board_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            f"/hcmUI/CandidateExperience/{language}/sites/{site_slug}",
            "",
            "",
            "",
        ))
        return board_url, site_number
