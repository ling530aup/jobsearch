"""SAP SuccessFactors public career-site adapter."""

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from .base import (
    CareerFetcher,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
)
from ..models import Job


class SuccessFactorsFetcher(CareerFetcher):
    """Fetch public jobs from a SuccessFactors external career site.

    SuccessFactors' supported OData APIs require tenant credentials.  Public
    career sites instead expose an unauthenticated browser session and load
    their search results through the site's JavaScript.  This adapter uses the
    public site only; it does not require or attempt to use tenant credentials.
    """

    MAX_PAGES = 100
    PAGE_WORKERS = 6

    def fetch_job_list(self) -> List[Job]:
        """Fetch all visible result pages from the public career site."""
        entry_url = self._resolve_entry_url()
        if not entry_url:
            return []

        # Branded SAP sites commonly server-render their complete pager.  Use
        # that public contract before opening a browser; it is both faster and
        # avoids generic code having to know SuccessFactors URL shapes.
        jobs = self._fetch_from_html(entry_url)
        if jobs:
            return jobs

        # Some tenants render only after JavaScript. Retain the browser
        # fallback for those boards, but never run it after a complete static
        # catalogue was obtained above.
        seen_urls = {job.canonical_url for job in jobs}
        for job in self._fetch_with_playwright(entry_url):
            if job.canonical_url not in seen_urls:
                seen_urls.add(job.canonical_url)
                jobs.append(job)
        return jobs

    def _resolve_entry_url(self) -> Optional[str]:
        """Return a SuccessFactors URL from a direct or embedded career URL."""
        if self._is_successfactors_url(self.career_url):
            return self.career_url

        try:
            response = self.client.get(self.career_url)
            if response.status_code != 200:
                return None
        except Exception:
            return None

        candidates = []
        pattern = r'''(?:href|src)=["']([^"']*successfactors\.(?:com|eu)[^"']*)["']'''
        for raw_url in re.findall(pattern, response.text, re.IGNORECASE):
            candidate = urljoin(self.career_url, html.unescape(raw_url))
            parsed = urlparse(candidate)
            path = parsed.path.casefold()
            # The first SuccessFactors URL on newer pages is often a CSS or
            # JavaScript CDN asset.  It is not a career tenant and previously
            # caused SAP to be sent to an unusable asset URL.
            if not re.search(r"/(?:career|portalcareer)(?:/|$)", path):
                continue
            if re.search(r"\.(?:css|js|png|jpg|jpeg|gif|svg|woff2?)(?:$|\?)", path):
                continue
            score = 0
            if "career_company=" in parsed.query.casefold() or "company=" in parsed.query.casefold():
                score += 3
            if "portalcareer" in path or re.search(r"/career(?:\?|$)", candidate, re.I):
                score += 2
            candidates.append((score, candidate))
        if not candidates:
            if self._is_branded_successfactors_page(response.text):
                return str(response.url)
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _is_successfactors_url(url: str) -> bool:
        return bool(re.search(r"\.successfactors\.(?:com|eu)(?:/|$)", url, re.I))

    @staticmethod
    def _is_branded_successfactors_page(page_html: str) -> bool:
        """Recognise public SAP Recruiting pages hosted on employer domains."""
        text = (page_html or "").casefold()
        return (
            "rmkcdn.successfactors.com" in text
            or "j2w.tc.init" in text
            or "platform/js/search/search.js" in text
        )

    def _fetch_from_html(self, entry_url: str) -> List[Job]:
        """Fetch and paginate public server-rendered SAP result pages."""
        try:
            # The initial request establishes the public career-site session.
            entry_response = self.client.get(entry_url)
            if entry_response.status_code != 200:
                return []
            search_url = self._find_search_url(
                entry_response.text,
                str(entry_response.url),
            ) or self._job_search_url(entry_url)
            response = self.client.get(search_url)
            if response.status_code == 200:
                jobs = self._parse_jobs(response.text, str(response.url))
                seen_urls = {job.canonical_url for job in jobs}
                page_urls = self._public_page_urls(
                    response.text,
                    str(response.url),
                )
                with ThreadPoolExecutor(
                    max_workers=min(self.PAGE_WORKERS, len(page_urls) or 1),
                ) as executor:
                    futures = [
                        executor.submit(self._fetch_html_page, page_url)
                        for page_url in page_urls
                    ]
                    for future in as_completed(futures):
                        if self.stop_requested():
                            break
                        for job in future.result():
                            if job.canonical_url not in seen_urls:
                                seen_urls.add(job.canonical_url)
                                jobs.append(job)
                return jobs
        except Exception:
            pass
        return []

    @staticmethod
    def _find_search_url(page_html: str, base_url: str) -> str:
        """Find the employer's public Search Jobs link from a SAP landing page."""
        for href, label in re.findall(
            r'''<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>''',
            page_html,
            re.IGNORECASE | re.DOTALL,
        ):
            text = " ".join(re.sub(r"<[^>]+>", "", label).split()).casefold()
            if (
                "search" in text and "job" in text
                and re.search(r"/go/search-jobs/", href, re.IGNORECASE)
            ):
                return urljoin(base_url, html.unescape(href))
        return ""

    def _fetch_html_page(self, page_url: str) -> List[Job]:
        try:
            response = self.client.get(page_url)
            if response.status_code == 200:
                return self._parse_jobs(response.text, str(response.url))
        except Exception:
            pass
        return []

    def _public_page_urls(self, page_html: str, current_url: str) -> List[str]:
        """Build finite public SAP offset URLs from the rendered pager."""
        total_match = re.search(
            r"paginationLabel[^>]*>.*?Results\s*<b>\s*1\s*[–-]\s*"
            r"(\d+)\s*</b>\s*of\s*<b>\s*([\d,]+)\s*</b>",
            page_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not total_match:
            return []
        page_size = int(total_match.group(1))
        total = int(total_match.group(2).replace(",", ""))
        page_two_match = re.search(
            r'''href=["']([^"']+)["'][^>]*title=["']Page\s+2["']''',
            page_html,
            re.IGNORECASE,
        )
        if page_size <= 0 or total <= page_size or not page_two_match:
            return []
        page_two_url = urljoin(current_url, html.unescape(page_two_match.group(1)))
        parsed = urlparse(page_two_url)
        offset_match = re.search(r"/(\d+)(?:/)?$", parsed.path)
        if not offset_match or int(offset_match.group(1)) != page_size:
            return []
        urls = []
        for offset in range(page_size, total, page_size):
            path = (
                parsed.path[:offset_match.start(1)]
                + str(offset)
                + parsed.path[offset_match.end(1):]
            )
            urls.append(urlunparse(parsed._replace(path=path)))
        return urls

    def _job_search_url(self, entry_url: str) -> str:
        """Build the standard external SuccessFactors job-search page URL."""
        parsed = urlparse(entry_url)
        query = parse_qs(parsed.query)
        company = query.get("company", [""])[0]
        if not company:
            return entry_url

        params = {
            "company": company,
            "career_company": company,
            "navBarLevel": "JOB_SEARCH",
        }
        language = query.get("lang", [""])[0]
        if language:
            params["lang"] = language
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            "/portalcareer",
            "",
            urlencode(params),
            "",
        ))

    def _fetch_with_playwright(self, entry_url: str) -> List[Job]:
        """Render JavaScript-driven SuccessFactors result pages and paginate."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        jobs: List[Job] = []
        seen_urls = set()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                install_browser_page_handlers(page)
                try:
                    goto_browser_page(page, entry_url, self.timeout)
                    goto_browser_page(
                        page,
                        self._job_search_url(entry_url),
                        self.timeout,
                    )
                    page.wait_for_timeout(2_000)
                    dismiss_browser_overlays(page)

                    for _ in range(self.MAX_PAGES):
                        if self.stop_requested():
                            break
                        dismiss_browser_overlays(page)
                        new_jobs = self._parse_jobs(page.content(), page.url)
                        added = 0
                        for job in new_jobs:
                            if job.canonical_url not in seen_urls:
                                seen_urls.add(job.canonical_url)
                                jobs.append(job)
                                added += 1

                        # A visually enabled Next button can keep serving
                        # the same page after a session expires. Do not spend
                        # all 100 iterations on repeated results.
                        if new_jobs and added == 0:
                            break

                        next_page = page.get_by_role("link", name=re.compile(r"^next$", re.I))
                        if next_page.count() == 0:
                            next_page = page.get_by_role("button", name=re.compile(r"^next$", re.I))
                        if next_page.count() == 0 or not next_page.first.is_enabled():
                            break

                        previous_url = page.url
                        next_page.first.click()
                        page.wait_for_timeout(1_000)
                        if page.url == previous_url and not self._parse_jobs(page.content(), page.url):
                            break
                finally:
                    browser.close()
        except Exception:
            return jobs
        return jobs

    def _parse_jobs(self, page_html: str, base_url: str) -> List[Job]:
        """Parse SuccessFactors job-detail links from a result page."""
        jobs = []
        seen_urls = set()
        link_pattern = r'''<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>'''

        for href, link_text in re.findall(link_pattern, page_html, re.DOTALL | re.I):
            url = urljoin(base_url, html.unescape(href))
            if not re.search(
                r"(?:jobreqid|jobid|job_req_id|jobpipeline)|/job/[^/]+/\d+/?$",
                url,
                re.I,
            ):
                continue

            title = re.sub(r"<[^>]+>", "", html.unescape(link_text))
            title = " ".join(title.split())
            if len(title) < 3:
                continue

            job = Job(
                company=self.company_name,
                title=title,
                url=url,
                source="successfactors",
            )
            if job.canonical_url in seen_urls:
                continue
            seen_urls.add(job.canonical_url)
            jobs.append(job)
        return jobs
