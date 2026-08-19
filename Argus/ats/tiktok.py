"""TikTok/ByteDance careers fetcher using Playwright."""

from typing import List

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
)
from ..models import Job


class TikTokFetcher(CareerFetcher):
    """Fetcher for TikTok careers using pagination via Playwright."""

    CAREERS_URL = "https://lifeattiktok.com/position"
    JOB_URL_TEMPLATE = "https://lifeattiktok.com/position/{job_id}"

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from TikTok careers by clicking through pagination."""
        jobs = []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
            return jobs

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = create_browser_context(browser)
                page = context.new_page()
                install_browser_page_handlers(page)

                all_jobs = []

                def handle_response(response):
                    if "search/job/posts" in response.url:
                        try:
                            data = response.json()
                            job_list = data.get("data", {}).get("job_post_list", [])
                            all_jobs.extend(job_list)
                        except Exception:
                            pass

                page.on("response", handle_response)

                # Load the careers page
                goto_browser_page(page, self.CAREERS_URL, self.timeout)
                page.wait_for_timeout(3000)
                dismiss_browser_overlays(page)

                # Click through pagination - find the highest page number available
                max_pages = 500
                current_page = 1

                while current_page < max_pages:
                    dismiss_browser_overlays(page)
                    current_page += 1

                    # Try to click the next page number
                    try:
                        next_btn = page.locator(f"button:text-is('{current_page}')").first
                        if next_btn.is_visible(timeout=2000):
                            next_btn.click()
                            page.wait_for_timeout(2000)
                        else:
                            # No more pages
                            break
                    except Exception:
                        break

                browser.close()

                # Convert results to Job objects
                seen_ids = set()
                for job_data in all_jobs:
                    job_id = job_data.get("id")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    title = job_data.get("title", "")
                    location = self._extract_location(job_data)
                    team = job_data.get("job_function_name", "")

                    if job_id and title:
                        jobs.append(Job(
                            company=self.company_name,
                            title=title,
                            url=self.JOB_URL_TEMPLATE.format(job_id=job_id),
                            location=location,
                            team=team,
                            source="tiktok",
                        ))

        except Exception as e:
            print(f"Error fetching TikTok jobs: {e}")

        return jobs

    def _extract_location(self, job_data: dict) -> str:
        """Extract location from city_info structure."""
        city_info = job_data.get("city_info", {})
        if not city_info:
            return ""

        parts = []

        # City
        city = city_info.get("en_name") or city_info.get("i18n_name", "")
        if city:
            parts.append(city)

        # State/Region
        parent = city_info.get("parent", {})
        if parent:
            state = parent.get("en_name") or parent.get("i18n_name", "")
            if state and state != city:  # Avoid duplicates like "Tokyo, Tokyo"
                parts.append(state)

            # Country
            grandparent = parent.get("parent", {})
            if grandparent:
                country = grandparent.get("en_name") or grandparent.get("i18n_name", "")
                if country and country != "United States of America":
                    parts.append(country)

        return ", ".join(parts) if parts else ""
