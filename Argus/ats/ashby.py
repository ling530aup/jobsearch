"""Ashby ATS adapter."""

import html
import re
from typing import List
from urllib.parse import urlparse

from .base import CareerFetcher
from ..models import Job


class AshbyFetcher(CareerFetcher):
    """Fetcher for Ashby ATS job boards."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self.company_slug = self._extract_company_slug()

    def _extract_company_slug(self) -> str:
        """Extract Ashby company slug from URL."""
        parsed = urlparse(self.career_url)

        if "ashbyhq.com" in parsed.netloc:
            # Direct Ashby URL: jobs.ashbyhq.com/companyname
            path_parts = parsed.path.strip("/").split("/")
            if path_parts:
                return path_parts[0]

        try:
            response = self.client.get(self.career_url)
            decoded = html.unescape(response.text).replace(r"\/", "/")
            match = re.search(
                r'''jobs\.ashbyhq\.com/([^/?#"'\\]+)''',
                decoded,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
        except Exception:
            pass

        # Fallback to company name slug
        return re.sub(r"[^a-z0-9]", "", self.company_name.casefold())

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs from Ashby GraphQL API."""
        jobs = []

        # Ashby GraphQL API endpoint
        api_url = "https://jobs.ashbyhq.com/api/non-user-graphql"

        # Updated query - jobPostings are now at the board level, not under teams
        query = {
            "operationName": "ApiJobBoardWithTeams",
            "variables": {
                "organizationHostedJobsPageName": self.company_slug
            },
            "query": """
                query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
                    jobBoard: jobBoardWithTeams(
                        organizationHostedJobsPageName: $organizationHostedJobsPageName
                    ) {
                        teams {
                            id
                            name
                        }
                        jobPostings {
                            id
                            title
                            locationName
                            employmentType
                            teamId
                        }
                    }
                }
            """
        }

        try:
            response = self.client.post(
                api_url,
                json=query,
                headers={"Content-Type": "application/json"}
            )
            if response.status_code == 200:
                data = response.json()
                jobs = self._parse_graphql_response(data)
            else:
                # Try fetching from HTML page
                jobs = self._fetch_from_html()
        except Exception as e:
            print(f"Error fetching Ashby jobs for {self.company_name}: {e}")
            jobs = self._fetch_from_html()

        return jobs

    def _parse_graphql_response(self, data: dict) -> List[Job]:
        """Parse jobs from GraphQL response."""
        jobs = []

        try:
            job_board = data.get("data", {}).get("jobBoard", {})
            teams = job_board.get("teams", [])
            job_postings = job_board.get("jobPostings", [])

            # Build team ID to name mapping
            team_map = {team.get("id", ""): team.get("name", "") for team in teams}

            for job_data in job_postings:
                job_id = job_data.get("id", "")
                team_id = job_data.get("teamId", "")
                team_name = team_map.get(team_id, "")

                job = Job(
                    company=self.company_name,
                    title=job_data.get("title", "Unknown"),
                    url=f"https://jobs.ashbyhq.com/{self.company_slug}/{job_id}",
                    location=job_data.get("locationName", ""),
                    team=team_name,
                    source="ashby",
                )
                jobs.append(job)
        except Exception:
            pass

        return jobs

    def _fetch_from_html(self) -> List[Job]:
        """Fetch jobs by parsing the Ashby HTML page."""
        jobs = []
        html_url = f"https://jobs.ashbyhq.com/{self.company_slug}"

        try:
            response = self.client.get(html_url)
            if response.status_code == 200:
                jobs = self._parse_html_jobs(response.text)
        except Exception:
            pass

        return jobs

    def _parse_html_jobs(self, html: str) -> List[Job]:
        """Parse jobs from HTML content."""
        jobs = []

        # Pattern for Ashby job postings
        pattern = r'href="(/[^/]+/[a-f0-9-]+)"[^>]*>.*?<[^>]+>([^<]+)</[^>]+>'
        matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)

        seen_urls = set()
        for path, title in matches:
            if path.startswith(f"/{self.company_slug}/"):
                url = f"https://jobs.ashbyhq.com{path}"
                if url not in seen_urls:
                    seen_urls.add(url)
                    jobs.append(Job(
                        company=self.company_name,
                        title=title.strip(),
                        url=url,
                        source="ashby",
                    ))

        return jobs
