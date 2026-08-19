"""Meta careers GraphQL fetcher using Playwright."""

from typing import List

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
    scroll_page_to_bottom,
)
from ..models import Job


class MetaFetcher(CareerFetcher):
    """Fetcher for Meta careers using GraphQL via Playwright."""

    CAREERS_URL = "https://www.metacareers.com/jobsearch"
    JOB_URL_TEMPLATE = "https://www.metacareers.com/jobs/{job_id}"

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from Meta's careers by intercepting GraphQL responses."""
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
                    if "graphql" in response.url:
                        try:
                            data = response.json()
                            # Extract jobs from GraphQL response
                            job_search = data.get("data", {}).get("job_search_with_featured_jobs", {})
                            job_list = job_search.get("all_jobs", [])
                            all_jobs.extend(job_list)
                        except Exception:
                            pass

                page.on("response", handle_response)

                # Load the careers page
                goto_browser_page(page, self.CAREERS_URL, self.timeout)
                page.wait_for_timeout(3000)
                dismiss_browser_overlays(page)

                # Scroll to load more jobs
                max_scrolls = 50
                prev_count = 0
                no_change_count = 0

                for _ in range(max_scrolls):
                    dismiss_browser_overlays(page)
                    # Scroll to bottom
                    if not scroll_page_to_bottom(page):
                        break
                    page.wait_for_timeout(1500)

                    # Check if we got new results
                    if len(all_jobs) == prev_count:
                        no_change_count += 1
                        if no_change_count >= 3:
                            break
                    else:
                        no_change_count = 0
                        prev_count = len(all_jobs)

                browser.close()

                # Convert results to Job objects
                seen_ids = set()
                for job_data in all_jobs:
                    job_id = job_data.get("id")
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    title = job_data.get("title", "")
                    locations = job_data.get("locations", [])
                    location = ", ".join(locations[:3]) if locations else ""
                    if len(locations) > 3:
                        location += f" +{len(locations) - 3} more"

                    teams = job_data.get("teams", [])
                    team = ", ".join(teams) if teams else ""

                    if job_id and title:
                        jobs.append(Job(
                            company=self.company_name,
                            title=title,
                            url=self.JOB_URL_TEMPLATE.format(job_id=job_id),
                            location=location,
                            team=team,
                            source="meta",
                        ))

        except Exception as e:
            print(f"Error fetching Meta jobs: {e}")

        return jobs
