"""Crawl orchestrator for job search agent."""

import yaml
from pathlib import Path
from typing import List, Optional

from .models import Company, Job
from .registry import CompanyRegistry
from .filter import JobFilter, LocationFilter
from .store import JobStore
from .ats import (
    ATSDetector,
    GreenhouseFetcher,
    LeverFetcher,
    AshbyFetcher,
    WorkdayFetcher,
    GenericFetcher,
    UberFetcher,
    AmazonFetcher,
    MetaFetcher,
    GoogleFetcher,
    TikTokFetcher,
)


class Orchestrator:
    """Orchestrates the job crawling process."""

    def __init__(
        self,
        companies_file: str,
        titles_file: str,
        output_dir: str = "job_results",
        timeout: float = 30.0,
    ):
        """Initialize orchestrator.

        Args:
            companies_file: Path to companies YAML file.
            titles_file: Path to job titles YAML file.
            output_dir: Directory for storing results.
            timeout: Timeout for HTTP requests in seconds.
        """
        self.companies_file = companies_file
        self.titles_file = titles_file
        self.output_dir = output_dir
        self.timeout = timeout

        self.registry = CompanyRegistry()
        self.store = JobStore(output_dir)
        self.detector = ATSDetector(timeout=timeout)

        # Load configuration
        self.companies = self._load_companies()
        self.target_titles, self.target_locations, self.exclude_levels = self._load_filters()
        self.title_filter = JobFilter(
            self.target_titles,
            exclude_levels=self.exclude_levels
        ) if self.target_titles else None
        self.location_filter = self._create_location_filter()

    def _load_companies(self) -> List[Company]:
        """Load companies from YAML file."""
        companies = []
        path = Path(self.companies_file)

        if not path.exists():
            print(f"Companies file not found: {self.companies_file}")
            return companies

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        for company_data in data.get("companies", []):
            company = Company(
                name=company_data.get("name"),
                career_url=company_data.get("career_url"),
                ats_type=company_data.get("ats_type"),
            )
            companies.append(company)

            # Update registry
            self.registry.add(company)

        return companies

    def _load_filters(self) -> tuple:
        """Load all filter values from the selected profile YAML file."""
        path = Path(self.titles_file)

        if not path.exists():
            print(f"Titles file not found: {self.titles_file}")
            return [], [], []

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Keep matching data-driven: filter.py receives the profile values as-is
        # instead of applying a separate built-in list of roles or locations.
        data = data or {}
        titles = data.get("titles", []) or []
        locations = data.get("locations", []) or []
        exclude_levels = data.get("exclude_levels", []) or []
        return titles, locations, exclude_levels

    def _create_location_filter(self) -> Optional[LocationFilter]:
        """Create location filter from config."""
        if not self.target_locations:
            return None

        # Check if 'remote' is in locations
        locations_lower = [str(loc).casefold() for loc in self.target_locations]
        allow_remote = "remote" in locations_lower

        # Filter out 'remote' from location list (handled separately).
        physical_locations = [
            loc for loc in self.target_locations if str(loc).casefold() != "remote"
        ]

        return LocationFilter(physical_locations, allow_remote=allow_remote)

    def _get_fetcher(self, company: Company):
        """Get appropriate fetcher for a company."""
        ats_type = company.ats_type

        # Detect ATS if not specified
        if not ats_type or ats_type == "unknown":
            print(f"  Detecting ATS for {company.name}...")
            ats_type = self.detector.detect(company.career_url)
            self.registry.update_ats_type(company.name, ats_type)
            print(f"  Detected: {ats_type}")

        # Return appropriate fetcher
        fetchers = {
            "greenhouse": GreenhouseFetcher,
            "lever": LeverFetcher,
            "ashby": AshbyFetcher,
            "workday": WorkdayFetcher,
            "uber": UberFetcher,
            "amazon": AmazonFetcher,
            "meta": MetaFetcher,
            "google": GoogleFetcher,
            "tiktok": TikTokFetcher,
        }

        fetcher_class = fetchers.get(ats_type, GenericFetcher)
        return fetcher_class(company.name, company.career_url, self.timeout)

    def crawl_company(self, company: Company) -> List[Job]:
        """Crawl jobs from a single company.

        Args:
            company: Company to crawl.

        Returns:
            List of Job objects found.
        """
        print(f"\nCrawling {company.name}...")

        try:
            with self._get_fetcher(company) as fetcher:
                jobs = fetcher.fetch_job_list()
                print(f"  Found {len(jobs)} jobs")

                # Filter by titles if configured
                if self.title_filter and jobs:
                    jobs = self.title_filter.filter_jobs(jobs)
                    print(f"  After title filter: {len(jobs)} matching jobs")

                # Filter by location if configured
                if self.location_filter and jobs:
                    jobs = self.location_filter.filter_jobs(jobs)
                    print(f"  After location filter: {len(jobs)} matching jobs")

                # Save to store
                new_count = self.store.save_jobs(jobs, company.name)
                print(f"  Saved {new_count} new jobs")

                # Update registry
                self.registry.update_last_crawled(company.name)

                return jobs

        except Exception as e:
            print(f"  Error crawling {company.name}: {e}")
            return []

    def run(self) -> dict:
        """Run the full crawl process.

        Returns:
            Summary statistics.
        """
        print(f"Starting job search agent")
        print(f"Companies: {len(self.companies)}")
        print(f"Target titles: {len(self.target_titles)}")
        if self.target_locations:
            print(f"Target locations: {', '.join(self.target_locations)}")
        if self.exclude_levels:
            print(f"Excluding levels: {', '.join(self.exclude_levels)}")
        print("-" * 50)

        total_jobs = 0
        successful_companies = 0
        failed_companies = []

        for company in self.companies:
            try:
                jobs = self.crawl_company(company)
                if jobs:
                    total_jobs += len(jobs)
                    successful_companies += 1
            except Exception as e:
                print(f"  Failed: {e}")
                failed_companies.append(company.name)

        # Cleanup
        self.detector.close()

        # Summary
        print("\n" + "=" * 50)
        print("SUMMARY")
        print("=" * 50)

        stats = self.store.get_stats()
        summary = {
            "companies_crawled": successful_companies,
            "companies_failed": len(failed_companies),
            "failed_companies": failed_companies,
            "total_matching_jobs": total_jobs,
            **stats,
        }

        print(f"Companies crawled: {successful_companies}/{len(self.companies)}")
        print(f"Total matching jobs found: {total_jobs}")
        print(f"Results saved to: {self.output_dir}")

        if failed_companies:
            print(f"Failed companies: {', '.join(failed_companies)}")

        return summary
