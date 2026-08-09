"""Job storage with file-based persistence."""

import csv
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import List, Set

from .models import Job


class JobStore:
    """Store job listings to files with deduplication."""

    def __init__(self, output_dir: str = "job_results"):
        """Initialize job store.

        Args:
            output_dir: Base directory for storing results.
        """
        self.output_dir = Path(output_dir)
        self._seen_jobs: Set[str] = set()
        self._lock = RLock()
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        """Load existing job URLs for deduplication."""
        if not self.output_dir.exists():
            return

        # Scan existing JSON files
        for json_file in self.output_dir.rglob("*.json"):
            try:
                with open(json_file, "r") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for job_data in data:
                            url = job_data.get("url", "")
                            if url:
                                self._seen_jobs.add(self._canonical_url(url))
            except Exception:
                pass

    def _canonical_url(self, url: str) -> str:
        """Create canonical URL for deduplication.

        Preserves job ID parameters for proper deduplication.
        """
        # Normalize case and trailing slashes, but keep query params
        # since they often contain the job ID (e.g., gh_jid, job_id)
        url = url.lower().rstrip("/")
        return url

    def _get_date_folder(self) -> Path:
        """Get folder path for today's date."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        return self.output_dir / date_str

    def _get_company_folder(self, company_name: str) -> Path:
        """Get folder path for a company."""
        # Sanitize company name for filesystem
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in company_name)
        safe_name = safe_name.strip().replace(" ", "_")
        return self._get_date_folder() / safe_name

    def save_jobs(self, jobs: List[Job], company_name: str) -> int:
        """Save jobs to storage.

        Args:
            jobs: List of Job objects to save.
            company_name: Name of the company.

        Returns:
            Number of new jobs saved (excluding duplicates).
        """
        if not jobs:
            return 0

        # Deduplication and file writes must be atomic when workers finish at
        # the same time, especially if two companies expose the same URL.
        with self._lock:
            new_jobs = []
            for job in jobs:
                canonical = self._canonical_url(job.url)
                if canonical not in self._seen_jobs:
                    self._seen_jobs.add(canonical)
                    new_jobs.append(job)

            if not new_jobs:
                return 0

            company_folder = self._get_company_folder(company_name)
            company_folder.mkdir(parents=True, exist_ok=True)
            self._save_json(new_jobs, company_folder / "jobs.json")
            self._save_csv(new_jobs, company_folder / "jobs.csv")
            return len(new_jobs)

    def _save_json(self, jobs: List[Job], path: Path) -> None:
        """Save jobs to JSON file."""
        existing = []
        if path.exists():
            try:
                with open(path, "r") as f:
                    existing = json.load(f)
            except Exception:
                pass

        # Append new jobs
        all_jobs = existing + [job.to_dict() for job in jobs]

        with open(path, "w") as f:
            json.dump(all_jobs, f, indent=2)

    def _save_csv(self, jobs: List[Job], path: Path) -> None:
        """Save jobs to CSV file."""
        # Check if file exists to determine if we need headers
        write_headers = not path.exists()

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            if write_headers:
                writer.writerow([
                    "company", "title", "location", "team", "url", "source", "discovered_at"
                ])

            for job in jobs:
                writer.writerow([
                    job.company,
                    job.title,
                    job.location or "",
                    job.team or "",
                    job.url,
                    job.source,
                    job.discovered_at,
                ])

    def get_stats(self) -> dict:
        """Get storage statistics."""
        total_jobs = len(self._seen_jobs)
        folders = list(self.output_dir.glob("*/*")) if self.output_dir.exists() else []

        return {
            "total_unique_jobs": total_jobs,
            "companies_crawled": len(folders),
            "output_directory": str(self.output_dir),
        }
