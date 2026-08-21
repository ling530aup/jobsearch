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
from .generic import GenericFetcher
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
                    content_type = (response.headers.get("content-type") or "").casefold()
                    if "graphql" not in response.url.casefold() and "json" not in content_type:
                        return
                    try:
                        data = response.json()
                        # Keep compatibility with the former exact GraphQL
                        # path, then scan the response for the current nested
                        # job records. Meta has changed the wrapper/query
                        # names several times without changing the public
                        # posting fields.
                        job_search = data.get("data", {}).get(
                            "job_search_with_featured_jobs", {}
                        ) if isinstance(data, dict) else {}
                        all_jobs.extend(job_search.get("all_jobs", []))
                        all_jobs.extend(self._extract_job_records(data))
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
                    if self.stop_requested():
                        break
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

                # Some deployments expose the jobs in rendered links even
                # when the GraphQL response is wrapped under a new key. Use
                # the shared link parser as an in-page, no-extra-request
                # fallback before closing the browser.
                if not all_jobs:
                    parser = GenericFetcher(
                        self.company_name,
                        page.url,
                        self.timeout,
                    )
                    all_jobs.extend({
                        "id": job.url.rsplit("/", 1)[-1],
                        "title": job.title,
                        "locations": [job.location] if job.location else [],
                    } for job in parser._extract_jobs_from_html(page.content(), page.url))

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

    @staticmethod
    def _extract_job_records(data) -> List[dict]:
        """Find Meta job-shaped records in changing GraphQL response wrappers."""
        records = []
        seen = set()
        title_keys = ("title", "job_title", "jobTitle", "name")
        id_keys = ("id", "job_id", "jobId", "requisition_id", "requisitionId")

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            title = next((value.get(key) for key in title_keys if value.get(key)), "")
            job_id = next((value.get(key) for key in id_keys if value.get(key)), "")
            keys = {str(key).casefold() for key in value}
            looks_like_job = bool(
                {"locations", "location", "teams", "team", "job_family"} & keys
            ) or any("job" in key or "requisition" in key for key in keys)
            if title and job_id and looks_like_job:
                marker = str(job_id)
                if marker not in seen:
                    seen.add(marker)
                    records.append(value)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)

        visit(data)
        return records
