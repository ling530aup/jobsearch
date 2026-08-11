"""Concurrent crawl orchestration for the job search agent."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from threading import Lock
from time import perf_counter
import yaml
from pathlib import Path
from typing import Callable, List, Optional

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

logger = logging.getLogger(__name__)


class Orchestrator:
    """Orchestrates the job crawling process."""

    def __init__(
        self,
        companies_file: str,
        titles_file: str,
        output_dir: str = "job_results",
        timeout: float = 30.0,
        progress_callback: Optional[Callable[[int, int, str, bool], None]] = None,
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
        self.progress_callback = progress_callback
        self._run_stats_lock = Lock()
        self._run_saved = 0

        self.registry = CompanyRegistry()
        self.store = JobStore(output_dir)
        # Load configuration
        self.companies = self._load_companies()
        (
            self.target_titles,
            self.target_locations,
            self.exclude_levels,
            self.max_workers,
        ) = self._load_filters()
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
            return [], [], [], 4

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # Keep matching data-driven: filter.py receives the profile values as-is
        # instead of applying a separate built-in list of roles or locations.
        data = data or {}
        titles = data.get("titles", []) or []
        locations = data.get("locations", []) or []
        exclude_levels = data.get("exclude_levels", []) or []
        try:
            max_workers = max(1, int(data.get("max_workers", 4)))
        except (TypeError, ValueError):
            logger.warning("Invalid max_workers in %s; using 4", self.titles_file)
            max_workers = 4
        return titles, locations, exclude_levels, max_workers

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
            logger.info("[%s] detecting ATS", company.name)
            # Each crawl worker owns its detector/client.  A shared detector
            # would require a global lock around network I/O and serialize
            # ATS detection for all companies.
            with ATSDetector(timeout=self.timeout) as detector:
                ats_type = detector.detect(company.career_url)
            self.registry.update_ats_type(company.name, ats_type)
            company.ats_type = ats_type
            logger.info("[%s] detected ATS=%s", company.name, ats_type)

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
        started_at = perf_counter()
        logger.info("[%s] crawl started", company.name)

        try:
            with self._get_fetcher(company) as fetcher:
                jobs = fetcher.fetch_job_list()
                fetched_count = len(jobs)

                # Keep company metadata with the domain objects. This lets
                # every persistence backend use one save_jobs(jobs) contract.
                for job in jobs:
                    job.career_url = company.career_url
                    job.ats_type = company.ats_type

                # Filter by titles if configured
                if self.title_filter and jobs:
                    jobs = self.title_filter.filter_jobs(jobs)
                title_count = len(jobs)

                # Filter by location if configured
                if self.location_filter and jobs:
                    jobs = self.location_filter.filter_jobs(jobs)
                location_count = len(jobs)

                # Save to store
                new_count = self.store.save_jobs(jobs)
                with self._run_stats_lock:
                    self._run_saved += new_count

                # Update registry
                self.registry.update_last_crawled(company.name)

                logger.info(
                    "[%s] crawl finished: fetched=%d title=%d location=%d new=%d elapsed=%.2fs",
                    company.name,
                    fetched_count,
                    title_count,
                    location_count,
                    new_count,
                    perf_counter() - started_at,
                )

                return jobs

        except Exception as e:
            logger.exception("[%s] crawl failed after %.2fs: %s", company.name, perf_counter() - started_at, e)
            return []

    def run(self) -> dict:
        """Run the full crawl process.

        Returns:
            Summary statistics.
        """
        logger.info("Starting job search agent")
        logger.info("Companies: %d", len(self.companies))
        logger.info("Max crawl workers: %d", self.max_workers)
        logger.info("Target titles: %d", len(self.target_titles))
        if self.target_locations:
            logger.info("Target locations: %s", ", ".join(self.target_locations))
        if self.exclude_levels:
            logger.info("Excluding levels: %s", ", ".join(self.exclude_levels))

        total_jobs = 0
        successful_companies = 0
        failed_companies = []
        with self._run_stats_lock:
            self._run_saved = 0
        run_id = self.store.mysql_store.start_crawl_run(len(self.companies))
        self.store.mysql_store.set_crawl_run_id(run_id)

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="crawl") as executor:
            futures = {
                executor.submit(self.crawl_company, company): company
                for company in self.companies
            }
            for future in as_completed(futures):
                company = futures[future]
                succeeded = False
                try:
                    jobs = future.result()
                    if jobs:
                        total_jobs += len(jobs)
                        successful_companies += 1
                        succeeded = True
                except Exception:
                    logger.exception("[%s] worker failed", company.name)
                    failed_companies.append(company.name)
                finally:
                    if self.progress_callback:
                        completed = len(futures) - sum(1 for item in futures if not item.done())
                        self.progress_callback(completed, len(futures), company.name, succeeded)

        # Cleanup
        self.store.mysql_store.finish_crawl_run(
            run_id,
            status="completed",
            companies_succeeded=successful_companies,
            companies_failed=len(failed_companies),
            jobs_fetched=total_jobs,
            jobs_saved=self._run_saved,
        )
        self.store.mysql_store.set_crawl_run_id(None)

        # Summary
        logger.info("SUMMARY")

        stats = self.store.get_stats()
        summary = {
            "run_id": run_id,
            "companies_crawled": successful_companies,
            "companies_failed": len(failed_companies),
            "failed_companies": failed_companies,
            "total_matching_jobs": total_jobs,
            **stats,
        }

        logger.info("Companies crawled: %d/%d", successful_companies, len(self.companies))
        logger.info("Total matching jobs found: %d", total_jobs)
        logger.info("Results saved to: %s", self.output_dir)

        if failed_companies:
            logger.error("Failed companies: %s", ", ".join(failed_companies))

        return summary
