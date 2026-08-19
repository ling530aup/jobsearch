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
    EightfoldFetcher,
    SuccessFactorsFetcher,
    WorkableFetcher,
    SmartRecruitersFetcher,
    OracleFetcher,
    RecruiteeFetcher,
    PersonioFetcher,
    BambooHRFetcher,
    TalentBrewFetcher,
    AppleFetcher,
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
        progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
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
        self._run_fetched = 0
        self._company_outcomes = {}

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
            name = company_data.get("name")
            career_url = company_data.get("career_url")
            ats_type = company_data.get("ats_type")
            detected = self.registry.get(name) if name else None
            cached_type = ATSDetector._check_url_patterns(
                detected.career_url
            ) if detected and detected.career_url else None
            cached_board_is_valid = bool(
                detected
                and detected.ats_type
                and detected.ats_type != "unknown"
                and cached_type == detected.ats_type
                and ATSDetector.is_public_board_url(
                    detected.career_url,
                    detected.ats_type,
                )
            )
            if (
                cached_board_is_valid
                and (
                    not ats_type
                    or ats_type == "unknown"
                    or ats_type == detected.ats_type
                )
            ):
                ats_type = detected.ats_type
                # A registry URL has already been resolved and validated as a
                # public ATS board. Reusing it avoids re-detecting hundreds of
                # corporate landing pages on every run. In particular, do not
                # let add_many() below replace that working URL with the
                # profile's corporate URL before crawling starts.
                career_url = detected.career_url
            company = Company(
                name=name,
                career_url=career_url,
                ats_type=ats_type,
            )
            companies.append(company)

        # Rewriting the complete registry once per company made startup
        # perform hundreds of serialized file writes for large profiles.
        self.registry.add_many(companies)

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
        effective_url = company.career_url

        # Resolve corporate career landing pages to the actual ATS board. This
        # matters even when ats_type is already configured: passing a corporate
        # hostname to a Workday/Greenhouse adapter produces a valid-looking but
        # completely wrong API URL.
        with ATSDetector(timeout=self.timeout) as detector:
            if not ats_type or ats_type in {"unknown", "generic", "custom"}:
                logger.info("[%s] detecting ATS", company.name)
                detected_type = detector.detect(company.career_url)
                if detected_type != "unknown" or not ats_type:
                    ats_type = detected_type
                    company.ats_type = ats_type
                logger.info("[%s] detected ATS=%s", company.name, detected_type)
            elif ats_type in detector.ATS_PATTERNS:
                detected_type = detector.detect(company.career_url)
                if detected_type != "unknown" and detected_type != ats_type:
                    logger.info(
                        "[%s] corrected stale ATS type %s -> %s",
                        company.name,
                        ats_type,
                        detected_type,
                    )
                    ats_type = detected_type
                    company.ats_type = ats_type
                elif detected_type == "unknown":
                    # A cached ATS type can become stale when a company
                    # changes careers vendors. Do not keep sending a stale
                    # adapter to a URL that current detection cannot verify.
                    logger.info(
                        "[%s] clearing stale ATS type %s; detection returned unknown",
                        company.name,
                        ats_type,
                    )
                    ats_type = "unknown"
                    company.ats_type = ats_type
            if ats_type in detector.ATS_PATTERNS:
                effective_url = detector.resolve_url(company.career_url, ats_type)
                if effective_url != company.career_url:
                    logger.info("[%s] resolved ATS URL: %s", company.name, effective_url)
            elif detector.resolved_url and detector.resolved_url != company.career_url:
                # The detector already followed a redirect. Reuse its final
                # page for GenericFetcher instead of starting another client
                # at the original URL and repeating the 301/302 chain.
                effective_url = detector.resolved_url
                logger.info(
                    "[%s] reusing final redirected URL: %s",
                    company.name,
                    effective_url,
                )

        # Do not persist a merely detected URL yet. Corporate pages often
        # contain links to a parent, subsidiary or vendor asset; cache the
        # resolution only after its fetcher proves that it returns jobs.
        company.ats_type = ats_type
        company.career_url = effective_url

        fetcher_class = self._fetcher_classes().get(ats_type, GenericFetcher)
        return fetcher_class(company.name, effective_url, self.timeout)

    @staticmethod
    def _fetcher_classes() -> dict:
        """Map detected ATS names to their complete-catalogue adapters."""
        return {
            "greenhouse": GreenhouseFetcher,
            "lever": LeverFetcher,
            "ashby": AshbyFetcher,
            "workday": WorkdayFetcher,
            "eightfold": EightfoldFetcher,
            "successfactors": SuccessFactorsFetcher,
            "workable": WorkableFetcher,
            "smartrecruiters": SmartRecruitersFetcher,
            "oracle": OracleFetcher,
            "recruitee": RecruiteeFetcher,
            "personio": PersonioFetcher,
            "bamboohr": BambooHRFetcher,
            "talentbrew": TalentBrewFetcher,
            "apple": AppleFetcher,
            "uber": UberFetcher,
            "amazon": AmazonFetcher,
            "meta": MetaFetcher,
            "google": GoogleFetcher,
            "tiktok": TikTokFetcher,
        }

    def crawl_company(self, company: Company) -> List[Job]:
        """Crawl jobs from a single company.

        Args:
            company: Company to crawl.

        Returns:
            List of Job objects found.
        """
        started_at = perf_counter()
        logger.info("[%s] crawl started", company.name)
        original_career_url = company.career_url

        try:
            with self._get_fetcher(company) as fetcher:
                jobs = fetcher.fetch_job_list()
                discovered_type = getattr(fetcher, "discovered_ats_type", "")
                discovered_url = getattr(fetcher, "discovered_ats_url", "")
                discovered_class = self._fetcher_classes().get(discovered_type)
                if (
                    discovered_class
                    and discovered_class is not fetcher.__class__
                    and discovered_url
                ):
                    logger.info(
                        "[%s] browser discovered ATS=%s at %s; retrying with %s",
                        company.name,
                        discovered_type,
                        discovered_url,
                        discovered_class.__name__,
                    )
                    with discovered_class(
                        company.name,
                        discovered_url,
                        self.timeout,
                    ) as discovered_fetcher:
                        discovered_jobs = discovered_fetcher.fetch_job_list()
                    if discovered_jobs:
                        jobs = discovered_jobs
                        company.ats_type = discovered_type
                        company.career_url = discovered_url
                if not jobs and not isinstance(fetcher, GenericFetcher):
                    fetcher_client = getattr(fetcher, "_client", None)
                    blocked_by_http = bool(
                        fetcher_client
                        and getattr(fetcher_client, "last_status_code", None)
                        in {403, 451}
                    )
                    fallback_url = (
                        original_career_url
                        if blocked_by_http
                        else fetcher.career_url
                    )
                    logger.info(
                        "[%s] %s returned no jobs%s; trying generic fallback at %s",
                        company.name,
                        fetcher.__class__.__name__,
                        " after HTTP block" if blocked_by_http else "",
                        fallback_url,
                    )
                    with GenericFetcher(
                        company.name,
                        fallback_url,
                        self.timeout,
                    ) as fallback:
                        jobs = fallback.fetch_job_list()
                fetched_count = len(jobs)
                if fetched_count:
                    self.registry.update_detection(
                        company.name,
                        company.ats_type or "unknown",
                        company.career_url,
                    )
                with self._run_stats_lock:
                    self._run_fetched += fetched_count

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

                if fetched_count == 0:
                    outcome = "fetch_empty"
                elif title_count == 0:
                    outcome = "title_filtered"
                elif location_count == 0:
                    outcome = "location_filtered"
                else:
                    outcome = "matched"
                with self._run_stats_lock:
                    self._company_outcomes[company.name] = outcome

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
            with self._run_stats_lock:
                self._company_outcomes[company.name] = "failed"
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
            self._run_fetched = 0
            self._company_outcomes = {}
        run_id = self.store.mysql_store.start_crawl_run(len(self.companies))
        self.store.mysql_store.set_crawl_run_id(run_id)

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="crawl") as executor:
            futures = {
                executor.submit(self.crawl_company, company): company
                for company in self.companies
            }
            for future in as_completed(futures):
                company = futures[future]
                outcome = "failed"
                try:
                    jobs = future.result()
                    with self._run_stats_lock:
                        outcome = self._company_outcomes.get(company.name, "failed")
                    if jobs:
                        total_jobs += len(jobs)
                        successful_companies += 1
                    if outcome == "failed":
                        failed_companies.append(company.name)
                except Exception:
                    logger.exception("[%s] worker failed", company.name)
                    failed_companies.append(company.name)
                finally:
                    if self.progress_callback:
                        completed = len(futures) - sum(1 for item in futures if not item.done())
                        self.progress_callback(completed, len(futures), company.name, outcome)

        # Cleanup
        companies_fetched = sum(
            1
            for outcome in self._company_outcomes.values()
            if outcome in {"matched", "title_filtered", "location_filtered"}
        )
        self.store.mysql_store.finish_crawl_run(
            run_id,
            status="completed",
            companies_succeeded=companies_fetched,
            companies_failed=len(failed_companies),
            jobs_fetched=self._run_fetched,
            jobs_saved=self._run_saved,
        )
        self.store.mysql_store.set_crawl_run_id(None)

        # Summary
        logger.info("SUMMARY")

        stats = self.store.get_stats()
        summary = {
            "run_id": run_id,
            "companies_crawled": companies_fetched,
            "companies_matched": successful_companies,
            "companies_failed": len(failed_companies),
            "failed_companies": failed_companies,
            "total_matching_jobs": total_jobs,
            "total_jobs_fetched": self._run_fetched,
            "outcomes": {
                outcome: sum(1 for value in self._company_outcomes.values() if value == outcome)
                for outcome in {
                    "matched", "title_filtered", "location_filtered", "fetch_empty", "failed"
                }
            },
            **stats,
        }

        logger.info("Companies fetched: %d/%d", companies_fetched, len(self.companies))
        logger.info("Companies with matching jobs: %d/%d", successful_companies, len(self.companies))
        logger.info("Total matching jobs found: %d", total_jobs)
        logger.info("Results saved to: %s", self.output_dir)

        if failed_companies:
            logger.error("Failed companies: %s", ", ".join(failed_companies))

        return summary
