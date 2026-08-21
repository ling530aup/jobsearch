"""Base interface for career page fetchers."""

from abc import ABC, abstractmethod
import logging
import time
from typing import List, Optional
import httpx

from ..models import Job


logger = logging.getLogger(__name__)


COOKIE_ACCEPT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
    "button[data-testid*='accept' i]",
    "button[id*='accept' i][id*='cookie' i]",
    "button[class*='accept' i][class*='cookie' i]",
)

COOKIE_ACCEPT_LABELS = (
    "Accept all", "Accept all cookies", "Allow all", "Allow all cookies",
    "I agree", "Agree and continue", "Accept cookies", "Accept Cookies",
    "Consent", "Got it",
)

DISMISS_LABELS = (
    "Dismiss", "Close", "No thanks", "No, thanks", "Not now",
    "Maybe later", "Continue without accepting",
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}


def is_access_challenge_page(page_html: str) -> bool:
    """Return whether a response is an access-verification interstitial.

    This intentionally identifies the interstitial itself rather than a page
    that merely embeds a CAPTCHA widget.  A crawler must not keep paginating
    a Cloudflare/Akamai verification document: it contains no public jobs and
    repeated requests make a later legitimate retry less likely to succeed.
    """
    text = (page_html or "").casefold()
    cloudflare_challenge = (
        "challenges.cloudflare.com" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
    )
    challenge_copy = any(
        marker in text
        for marker in (
            "just a moment...",
            "enable javascript and cookies to continue",
            "attention required | cloudflare",
            "checking your browser before accessing",
        )
    )
    return cloudflare_challenge and challenge_copy


def is_access_challenge_response(response) -> bool:
    """Return whether response metadata explicitly identifies a WAF challenge."""
    try:
        waf_action = str(response.headers.get("x-amzn-waf-action", "")).casefold()
        status_code = response.status_code
    except (AttributeError, TypeError):
        return False
    # AWS WAF's challenge response is intentionally an empty HTTP 202, so it
    # cannot be recognised from page text. It is an access-verification flow,
    # not an asynchronously accepted jobs request.
    return status_code == 202 and waf_action == "challenge"


def install_browser_page_handlers(page) -> None:
    """Install non-blocking handlers before navigating a Playwright page."""
    page.on("dialog", lambda dialog: dialog.accept())


def goto_browser_page(page, url: str, timeout: float):
    """Navigate without treating a slow load event as a failed page.

    Many careers pages continue loading analytics, chat and personalization
    resources indefinitely. ``page.goto(..., wait_until="domcontentloaded")``
    can therefore raise even though the document is already usable. Commit
    means the response/navigation started; the short best-effort load-state
    wait gives normal pages time to finish without blocking job extraction.
    """
    response = page.goto(
        url,
        wait_until="commit",
        timeout=timeout * 1000,
    )
    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=min(timeout * 1000, 8_000),
        )
    except Exception:
        pass
    return response


def create_browser_context(browser):
    """Create a normal browser context for sites that reject bare HTTP clients."""
    return browser.new_context(
        user_agent=BROWSER_USER_AGENT,
        locale="en-US",
        viewport={"width": 1440, "height": 900},
        extra_http_headers=BROWSER_HEADERS,
    )


def dismiss_browser_overlays(page) -> int:
    """Accept cookie consent and close common modal overlays when present.

    Career sites use many consent providers and often place them in iframes.
    Keep this best-effort and narrowly target buttons so application controls
    are never clicked accidentally.
    """
    clicked = 0
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    targets = [page]
    try:
        targets.extend(frame for frame in page.frames if frame != page.main_frame)
    except Exception:
        pass

    for target in targets:
        accepted = False
        for selector in COOKIE_ACCEPT_SELECTORS:
            try:
                control = target.locator(selector).first
                if control.count() and control.is_visible(timeout=150):
                    control.click(timeout=1_000)
                    clicked += 1
                    accepted = True
                    break
            except Exception:
                continue
        if not accepted:
            for label in COOKIE_ACCEPT_LABELS:
                try:
                    control = target.get_by_role("button", name=label, exact=True).first
                    if control.count() and control.is_visible(timeout=100):
                        control.click(timeout=1_000)
                        clicked += 1
                        accepted = True
                        break
                except Exception:
                    continue

        # Cookie consent takes priority. Then close newsletter, locale and
        # sign-in promotions which can cover pagination controls.
        for label in DISMISS_LABELS:
            try:
                control = target.get_by_role("button", name=label, exact=True).first
                if control.count() and control.is_visible(timeout=100):
                    control.click(timeout=1_000)
                    clicked += 1
                    break
            except Exception:
                continue
    return clicked


def scroll_page_to_bottom(page) -> bool:
    """Scroll a page when its document has a usable scrolling element.

    During navigation some sites briefly expose a document without a body.
    Calling ``document.body.scrollHeight`` in that window raises a Playwright
    ``TypeError`` and can abort an otherwise recoverable crawl.
    """
    try:
        return bool(page.evaluate(
            """() => {
                const scrollingElement = document.scrollingElement
                    || document.documentElement
                    || document.body;
                if (!scrollingElement) return false;
                window.scrollTo(0, scrollingElement.scrollHeight || 0);
                return true;
            }"""
        ))
    except Exception:
        # A page can also be replaced/detached while a navigation is in
        # progress. Treat that like a missing scrolling element; the caller
        # decides whether to retry navigation or stop this scroll pass.
        return False


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
    def __init__(
        self,
        *args,
        retry_attempts: int = 2,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.retry_attempts = max(1, retry_attempts)
        self.last_status_code = None
        self.stop_event = None
        self._insecure_certificate_client = None

    def _ensure_not_cancelled(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("company crawl cancelled")

    def request(self, method: str, url, **kwargs):
        for attempt in range(self.retry_attempts):
            self._ensure_not_cancelled()
            response = None
            try:
                response = super().request(method, url, **kwargs)
                self.last_status_code = response.status_code
                if response.status_code in {403, 451}:
                    # A 403 is generally a policy decision, not a transient
                    # transport failure. Do not hammer the host with retries;
                    # callers can fall back to the browser path instead. Do
                    # not impose a host-wide delay either: with many workers,
                    # one blocked company must not stall unrelated requests.
                    return response
                if response.status_code not in self.RETRYABLE_STATUS_CODES:
                    return response
                if attempt == self.retry_attempts - 1:
                    return response
            except self.RETRYABLE_EXCEPTIONS:
                if self._is_certificate_verification_error():
                    response = self._request_with_system_certificate_fallback(
                        method, url, **kwargs,
                    )
                    self.last_status_code = response.status_code
                    return response
                if attempt == self.retry_attempts - 1:
                    raise
            retry_after = 0.0
            if response is not None:
                try:
                    retry_after = min(float(response.headers.get("Retry-After", 0)), 10.0)
                except (TypeError, ValueError):
                    retry_after = 0.0
            time.sleep(max(retry_after, 0.4 * (2 ** attempt)))

    @staticmethod
    def _is_certificate_verification_error() -> bool:
        """Return whether the active exception is a missing local CA chain."""
        import sys
        exception = sys.exception()
        return "certificate_verify_failed" in str(exception).casefold()

    def _request_with_system_certificate_fallback(self, method: str, url, **kwargs):
        """Retry only CA-chain failures without changing normal HTTP policy.

        Some public career portals ship an incomplete intermediate chain that
        Chromium accepts through the OS store while Python/httpx rejects.  A
        one-time fallback keeps those pages on the fast HTTP path instead of
        forcing slow browser pagination. It is never used for HTTP status
        errors or ordinary transport failures.
        """
        if self._insecure_certificate_client is None:
            logger.warning(
                "TLS verification failed for a public career endpoint; "
                "retrying that request without certificate validation"
            )
            self._insecure_certificate_client = httpx.Client(
                verify=False,
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                ),
                headers=dict(self.headers),
                follow_redirects=self.follow_redirects,
                max_redirects=self.max_redirects,
            )
        return self._insecure_certificate_client.request(method, url, **kwargs)

    def close(self) -> None:
        if self._insecure_certificate_client is not None:
            self._insecure_certificate_client.close()
            self._insecure_certificate_client = None
        super().close()


def create_http_client(timeout: float) -> RetryingClient:
    """Create the shared HTTP policy used by ATS detection and fetching."""
    return RetryingClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
        ),
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
        follow_redirects=True,
        # Normal career sites need only a couple of hops (HTTP -> HTTPS,
        # apex -> www, then the careers page). Bound malformed redirect loops
        # so one company cannot consume the full request timeout repeatedly.
        max_redirects=8,
        # One retry is enough for a transient 5xx/connection reset while
        # avoiding the large latency multiplier when many companies are
        # unavailable.  403/404 are returned immediately above.
        retry_attempts=2,
    )


class CareerFetcher(ABC):
    """Abstract base class for ATS-specific job fetchers."""

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        self.company_name = company_name
        self.career_url = career_url
        self.timeout = timeout
        self._client: Optional[httpx.Client] = None
        self._stop_event = None

    @property
    def client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = create_http_client(self.timeout)
            self._client.stop_event = self._stop_event
        return self._client

    def set_stop_event(self, stop_event) -> None:
        """Attach the orchestrator cancellation signal to this fetcher."""
        self._stop_event = stop_event
        if self._client is not None:
            self._client.stop_event = stop_event

    def stop_requested(self) -> bool:
        """Return whether the enclosing crawl run was cancelled."""
        return bool(self._stop_event is not None and self._stop_event.is_set())

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
