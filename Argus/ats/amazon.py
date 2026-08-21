"""Amazon jobs API fetcher."""

from typing import List

from .base import CareerFetcher
from ..models import Job


class AmazonFetcher(CareerFetcher):
    """Fetcher for Amazon jobs using their JSON API."""

    API_URL = "https://www.amazon.jobs/en/search.json"
    JOB_URL_TEMPLATE = "https://www.amazon.jobs/en/jobs/{job_id}"

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)

    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from Amazon's jobs API."""
        jobs = []
        offset = 0
        limit = 100

        while True:
            params = {
                "radius": "24km",
                "offset": offset,
                "result_limit": limit,
                "sort": "relevant",
            }

            try:
                response = self.client.get(self.API_URL, params=params)

                if response.status_code != 200:
                    break

                data = response.json()
                job_list = data.get("jobs", [])

                if not job_list:
                    break

                new_jobs = 0
                known_urls = {job.canonical_url for job in jobs}
                for job_data in job_list:
                    job_id = job_data.get("id_icims")
                    title = job_data.get("title", "")
                    location = job_data.get("location", "")
                    team = job_data.get("business_category", "")

                    if job_id and title:
                        job = Job(
                            company=self.company_name,
                            title=title,
                            url=self.JOB_URL_TEMPLATE.format(job_id=job_id),
                            location=location,
                            team=team,
                            source="amazon",
                        )
                        if job.canonical_url in known_urls:
                            continue
                        known_urls.add(job.canonical_url)
                        jobs.append(job)
                        new_jobs += 1

                # Check if we've fetched all jobs
                total_hits = data.get("hits", 0)
                if len(jobs) >= total_hits or len(job_list) < limit:
                    break

                if new_jobs == 0:
                    # The endpoint accepted a new offset but returned the
                    # same page; further offsets would only repeat it.
                    break

                offset += limit

                # Safety limit to prevent infinite loops
                if offset > 15000:
                    break

            except Exception as e:
                print(f"Error fetching Amazon jobs at offset {offset}: {e}")
                break

        return jobs
