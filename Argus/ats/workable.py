"""Workable public job-board adapter."""

import html
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from .base import CareerFetcher
from ..models import Job


class WorkableFetcher(CareerFetcher):
    """Fetch published jobs from Workable's public account endpoint."""

    def fetch_job_list(self) -> List[Job]:
        board_url = self._resolve_board_url()
        if not board_url:
            return []
        slug = self._extract_slug(board_url)
        if not slug:
            return []

        api_url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
        payload = {
            "query": "",
            "location": [],
            "department": [],
            "worktype": [],
            "remote": [],
        }
        try:
            response = self.client.post(
                api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if response.status_code != 200:
                return []
            results = response.json().get("results", [])
        except Exception:
            return []

        jobs = []
        for item in results if isinstance(results, list) else []:
            title = item.get("title")
            shortcode = item.get("shortcode")
            if not title or not shortcode:
                continue
            locations = item.get("locations") or [item.get("location") or {}]
            location_names = []
            for location in locations:
                if not isinstance(location, dict):
                    continue
                value = ", ".join(
                    str(location.get(key))
                    for key in ("city", "region", "country")
                    if location.get(key)
                )
                if value and value not in location_names:
                    location_names.append(value)
            if item.get("remote") and "Remote" not in location_names:
                location_names.append("Remote")
            departments = item.get("department") or []
            jobs.append(Job(
                company=self.company_name,
                title=title,
                url=f"https://apply.workable.com/{slug}/j/{shortcode}/",
                location="; ".join(location_names),
                team=", ".join(str(value) for value in departments),
                source="workable",
            ))
        return jobs

    def _resolve_board_url(self) -> Optional[str]:
        if self._extract_slug(self.career_url):
            return self.career_url
        try:
            response = self.client.get(self.career_url)
        except Exception:
            return None
        match = re.search(
            r'''(?:href|src)=["']([^"']*apply\.workable\.com/[^"']+)["']''',
            response.text,
            re.IGNORECASE,
        )
        return urljoin(self.career_url, html.unescape(match.group(1))) if match else None

    @staticmethod
    def _extract_slug(url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc.casefold() != "apply.workable.com":
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        return parts[0] if parts else ""
