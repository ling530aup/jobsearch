"""Eightfold public career-site adapter."""

import html
import json
import re
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from .base import CareerFetcher
from ..models import Job


class EightfoldFetcher(CareerFetcher):
    """Fetch all publicly listed jobs from Eightfold career portals."""

    PAGE_SIZE = 10
    MAX_POSITIONS = 10_000

    def fetch_job_list(self) -> List[Job]:
        entry_url = self._resolve_entry_url()
        if not entry_url:
            return []

        try:
            response = self.client.get(entry_url)
            if response.status_code != 200:
                return []
            initial_data = self._parse_embedded_data(response.text)
        except Exception:
            return []
        if not initial_data:
            return []

        domain = initial_data.get("domain") or parse_qs(urlparse(entry_url).query).get("domain", [""])[0]
        if not domain:
            return []

        api_url = urljoin(entry_url, "/api/apply/v2/jobs")
        jobs = []
        seen_ids = set()
        offset = 0
        total = None

        while offset < self.MAX_POSITIONS:
            try:
                response = self.client.get(
                    api_url,
                    params={"domain": domain, "start": offset, "num": self.PAGE_SIZE},
                )
                if response.status_code != 200:
                    break
                data = response.json()
            except Exception:
                break

            positions = data.get("positions", [])
            if not isinstance(positions, list) or not positions:
                break
            if total is None and isinstance(data.get("count"), int):
                total = data["count"]

            for position in positions:
                position_id = position.get("id")
                if not position_id or position_id in seen_ids:
                    continue
                seen_ids.add(position_id)
                title = position.get("posting_name") or position.get("name")
                if not title:
                    continue
                locations = position.get("locations") or []
                location = ", ".join(str(item) for item in locations) if locations else position.get("location", "")
                team = position.get("department") or position.get("business_unit") or ""
                jobs.append(Job(
                    company=self.company_name,
                    title=title,
                    url=position.get("canonicalPositionUrl") or urljoin(entry_url, f"/careers/job/{position_id}"),
                    location=location,
                    team=team,
                    source="eightfold",
                ))

            offset += len(positions)
            if len(positions) < self.PAGE_SIZE or (total is not None and offset >= total):
                break

        return jobs

    def _resolve_entry_url(self) -> Optional[str]:
        """Resolve a direct or embedded Eightfold career portal URL."""
        parsed = urlparse(self.career_url)
        if ".eightfold.ai" in parsed.netloc or parsed.netloc.startswith("portal.careers."):
            return self.career_url

        try:
            response = self.client.get(self.career_url)
        except Exception:
            return None
        if response.status_code != 200:
            return None

        match = re.search(
            r'''href=["']([^"']*(?:\.eightfold\.ai|portal\.careers\.)[^"']*/careers[^"']*)["']''',
            response.text,
            re.IGNORECASE,
        )
        return urljoin(self.career_url, html.unescape(match.group(1))) if match else None

    @staticmethod
    def _parse_embedded_data(page_html: str) -> Optional[dict]:
        match = re.search(
            r'<code id="smartApplyData"[^>]*>(.*?)</code>',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return None
        try:
            return json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            return None
