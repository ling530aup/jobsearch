"""Data models for job search agent."""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json
from urllib.parse import parse_qsl, urlparse, urlunparse


@dataclass
class Company:
    """Represents a company to search for jobs."""

    name: str
    career_url: str
    ats_type: Optional[str] = None
    last_crawled: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Company":
        """Create Company from dictionary."""
        return cls(**data)


@dataclass
class Job:
    """Represents a job listing."""

    company: str
    title: str
    url: str
    location: Optional[str] = None
    team: Optional[str] = None
    source: str = "unknown"
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    applied: bool = False
    # Crawl metadata is copied onto the job before persistence so storage
    # layers do not need duplicate company arguments.
    career_url: Optional[str] = None
    ats_type: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        """Create Job from dictionary."""
        return cls(**data)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @property
    def canonical_url(self) -> str:
        """Get canonical URL for deduplication."""
        parsed = urlparse(self.url)
        # A number of older and white-label career sites put the only stable
        # requisition identifier in the query string.  Dropping the complete
        # query made every result on URLs such as ``/job?jobId=...`` collapse
        # into one job.  Keep only known identity fields; tracking and search
        # parameters are still discarded.
        job_identifier_keys = {
            "jobreqid",
            "jobid",
            "job_req_id",
            "jobpipeline",
            "positionid",
            "requisitionid",
            "reqid",
            "job_id",
        }
        job_identifiers = sorted(
            (key.casefold(), value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() in job_identifier_keys and value
        )
        if job_identifiers:
            query = "&".join(f"{key}={value}" for key, value in job_identifiers)
            return urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/"),
                "",
                query,
                "",
            )).casefold()

        # Remove query parameters and trailing slashes
        url = self.url.split("?")[0].rstrip("/")
        return url.lower()
