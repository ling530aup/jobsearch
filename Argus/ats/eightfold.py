"""Eightfold public career-site adapter."""

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from .base import CareerFetcher
from ..models import Job


class EightfoldFetcher(CareerFetcher):
    """Fetch all publicly listed jobs from Eightfold career portals."""

    PAGE_SIZE = 10
    PAGE_WORKERS = 3
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

        if initial_data.get("configs") or initial_data.get("pcsDomain"):
            return self._fetch_modern_pcsx(entry_url, initial_data)

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

    def _fetch_modern_pcsx(self, entry_url: str, initial_data: dict) -> List[Job]:
        """Fetch current Eightfold Candidate Experience (PCSX) search pages."""
        domain = initial_data.get("domain")
        if not domain:
            return []
        configs = initial_data.get("configs") or {}
        pcs_domain = initial_data.get("pcsDomain") or configs.get("pcsDomain")
        if not pcs_domain:
            # pcsDomain can be nested in tenant configuration.
            def find_pcs_domain(value):
                if isinstance(value, dict):
                    if value.get("pcsDomain"):
                        return value["pcsDomain"]
                    for nested in value.values():
                        found = find_pcs_domain(nested)
                        if found:
                            return found
                elif isinstance(value, list):
                    for nested in value:
                        found = find_pcs_domain(nested)
                        if found:
                            return found
                return ""

            pcs_domain = find_pcs_domain(configs)
        api_origin = (
            f"https://{str(pcs_domain).strip('/')}"
            if pcs_domain
            else f"{urlparse(entry_url).scheme}://{urlparse(entry_url).netloc}"
        )
        api_url = f"{api_origin}/api/pcsx/search"
        def fetch_page(offset: int):
            try:
                response = self.client.get(
                    api_url,
                    params={
                        "domain": domain,
                        "query": "",
                        "location": "",
                        "start": offset,
                    },
                )
                if response.status_code != 200:
                    return {}, []
                payload = response.json().get("data") or {}
            except Exception:
                return {}, []
            positions = payload.get("positions") or []
            return payload, positions if isinstance(positions, list) else []

        first_payload, first_positions = fetch_page(0)
        if not first_positions:
            return []
        total = first_payload.get("count")
        page_size = len(first_positions)
        pages = [(0, first_positions)]
        if isinstance(total, int) and total > page_size:
            offsets = range(page_size, min(total, self.MAX_POSITIONS), page_size)
            with ThreadPoolExecutor(
                max_workers=self.PAGE_WORKERS,
                thread_name_prefix="eightfold-page",
            ) as executor:
                pages.extend(
                    (offset, positions)
                    for offset, (_payload, positions) in zip(
                        offsets,
                        executor.map(fetch_page, offsets),
                    )
                )

        jobs = []
        seen_ids = set()
        for _offset, positions in sorted(pages, key=lambda item: item[0]):
            if not positions:
                continue

            for position in positions:
                position_id = position.get("id")
                title = position.get("name") or position.get("posting_name")
                if not position_id or not title or position_id in seen_ids:
                    continue
                seen_ids.add(position_id)
                locations = position.get("locations") or []
                location = (
                    "; ".join(str(item) for item in locations)
                    if isinstance(locations, list)
                    else str(locations)
                )
                position_url = position.get("positionUrl") or f"/careers/job/{position_id}"
                jobs.append(Job(
                    company=self.company_name,
                    title=str(title),
                    url=urljoin(api_origin, str(position_url)),
                    location=location,
                    team=str(position.get("department") or position.get("business_unit") or ""),
                    source="eightfold-pcsx",
                ))
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

        if self._parse_embedded_data(response.text):
            return str(response.url)

        match = re.search(
            r'''href=["']([^"']*(?:\.eightfold\.ai|portal\.careers\.)[^"']*/careers[^"']*)["']''',
            response.text,
            re.IGNORECASE,
        )
        return urljoin(self.career_url, html.unescape(match.group(1))) if match else None

    @staticmethod
    def _parse_embedded_data(page_html: str) -> Optional[dict]:
        match = re.search(
            r'''<code[^>]+id=["'](?:smartApplyData|pcsx-data)["'][^>]*>(.*?)</code>''',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return None
        try:
            return json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            return None
