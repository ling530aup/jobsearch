"""Uber careers API fetcher using Playwright."""

from typing import List

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
)
from ..models import Job


class UberFetcher(CareerFetcher):
    """Fetcher for Uber careers using their internal API via Playwright."""

    CAREERS_URL = "https://www.uber.com/us/en/careers/list/"
    JOB_URL_TEMPLATE = "https://www.uber.com/us/en/careers/list/{job_id}/"

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from Uber's careers by intercepting API responses."""
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

                all_results = []

                def handle_response(response):
                    if "loadSearchJobsResults" in response.url:
                        try:
                            data = response.json()
                            results = data.get("data", {}).get("results", [])
                            all_results.extend(results)
                        except Exception:
                            pass

                page.on("response", handle_response)

                # Load the careers page
                goto_browser_page(page, self.CAREERS_URL, self.timeout)
                page.wait_for_timeout(3000)
                dismiss_browser_overlays(page)

                # Click "Show more openings" button repeatedly to load all jobs
                max_clicks = 100
                for _ in range(max_clicks):
                    dismiss_browser_overlays(page)
                    try:
                        show_more = page.locator("button:has-text('Show more openings')")
                        if show_more.is_visible(timeout=2000):
                            show_more.click()
                            page.wait_for_timeout(1500)
                        else:
                            break
                    except Exception:
                        break

                browser.close()

                # Convert results to Job objects
                seen_ids = set()
                for job_data in all_results:
                    job_id = job_data.get("id")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    title = job_data.get("title", "")
                    location_data = job_data.get("location", {})
                    location = self._format_location(location_data)
                    team = job_data.get("team", "")

                    if job_id and title:
                        jobs.append(Job(
                            company=self.company_name,
                            title=title,
                            url=self.JOB_URL_TEMPLATE.format(job_id=job_id),
                            location=location,
                            team=team,
                            source="uber",
                        ))

        except Exception as e:
            print(f"Error fetching Uber jobs: {e}")

        return jobs

    def _format_location(self, location_data) -> str:
        """Format location dictionary into a string."""
        if not location_data:
            return ""
        if isinstance(location_data, str):
            return location_data

        parts = []
        city = location_data.get("city", "")
        region = location_data.get("region", "")
        country_name = location_data.get("countryName", "")

        if city:
            parts.append(city)
        if region:
            parts.append(region)
        if country_name and country_name != "United States":
            parts.append(country_name)

        return ", ".join(parts) if parts else ""
