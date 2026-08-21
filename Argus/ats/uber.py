"""Uber's current server-rendered careers board adapter."""

from typing import List

from .generic import GenericFetcher
from ..models import Job


class UberFetcher(GenericFetcher):
    """Use the shared static/pagination extractor for Uber's current board.

    Uber moved away from the legacy ``loadSearchJobsResults`` response used by
    the old adapter.  The current ``jobs.uber.com`` board renders ordinary
    HTML job links and paginates with ``?page=&pagesize=`` links, so keeping a
    separate XHR listener silently returns zero jobs after that migration.
    """

    def fetch_job_list(self) -> List[Job]:
        jobs = super().fetch_job_list()
        for job in jobs:
            job.source = "uber"
        return jobs
