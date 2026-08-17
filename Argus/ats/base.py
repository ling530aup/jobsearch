"""Base interface for career page fetchers."""

from abc import ABC, abstractmethod
import time
from typing import List, Optional
import httpx

from ..models import Job


class RetryingClient(httpx.Client):
    """HTTP client with bounded retries for transient crawler failures."""

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    RETRYABLE_EXCEPTIONS = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
    )

    def __init__(self, *args, retry_attempts: int = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_attempts = max(1, retry_attempts)

    def request(self, method: str, url, **kwargs):
        for attempt in range(self.retry_attempts):
            try:
                response = super().request(method, url, **kwargs)
                if response.status_code not in self.RETRYABLE_STATUS_CODES:
                    return response
                if attempt == self.retry_attempts - 1:
                    return response
            except self.RETRYABLE_EXCEPTIONS:
                if attempt == self.retry_attempts - 1:
                    raise
            time.sleep(0.4 * (2 ** attempt))


def create_http_client(timeout: float) -> RetryingClient:
    """Create the shared HTTP policy used by ATS detection and fetching."""
    return RetryingClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
        ),
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
        follow_redirects=True,
        retry_attempts=3,
    )


class CareerFetcher(ABC):
    """Abstract base class for ATS-specific job fetchers."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        self.company_name = company_name
        self.career_url = career_url
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = create_http_client(self.timeout)
        return self._client

    @abstractmethod
    def fetch_job_list(self) -> List[Job]:
        """Fetch all jobs from the career page.

        Returns:
            List of Job objects found on the career page.
        """
        pass

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "CareerFetcher":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
