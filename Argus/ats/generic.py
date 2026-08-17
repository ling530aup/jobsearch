"""Generic career page fetcher using Playwright."""

import json
import re
from typing import List
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .base import CareerFetcher
from ..models import Job


class GenericFetcher(CareerFetcher):
    """Generic fetcher using Playwright for JavaScript-rendered pages."""

    # Keywords that indicate job-related links
    JOB_KEYWORDS = [
        "job", "career", "position", "role", "opening",
        "opportunity", "apply", "hiring", "vacancy"
    ]

    # Patterns to exclude
    EXCLUDE_PATTERNS = [
        r"/blog/", r"/news/", r"/about/", r"/contact/",
        r"/privacy", r"/terms", r"/legal/", r"/login",
        r"/signin", r"/signup", r"\.pdf$", r"\.png$",
        r"\.jpg$", r"javascript:", r"mailto:", r"tel:"
    ]
    # Some large employers expose thousands of roles as server-rendered pages.
    # Keep the ceiling finite to avoid an accidental pagination loop, while
    # allowing 15-role pages such as Citi's complete catalogue to finish.
    MAX_STATIC_PAGES = 300
    MAX_BROWSER_PAGES = 100

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self._playwright = None
        self._browser = None

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs using Playwright for full page rendering."""
        # First try simple HTTP fetch
        jobs = self._fetch_simple()
        if jobs:
            return jobs

        # Fall back to Playwright
        jobs = self._fetch_with_playwright()
        return jobs

    def _fetch_simple(self) -> List[Job]:
        """Fetch static job pages, following same-site next-page links."""
        jobs = []
        seen_job_urls = set()
        seen_pages = set()
        page_url = self.career_url

        for _ in range(self.MAX_STATIC_PAGES):
            if page_url in seen_pages:
                break
            seen_pages.add(page_url)

            try:
                response = self.client.get(page_url)
            except Exception:
                break
            if response.status_code != 200:
                break

            for job in self._extract_jobs_from_html(response.text, str(response.url)):
                if job.canonical_url not in seen_job_urls:
                    seen_job_urls.add(job.canonical_url)
                    jobs.append(job)

            page_url = self._find_next_page_url(response.text, str(response.url))
            if not page_url:
                break

        return jobs

    def _find_next_page_url(self, page_html: str, current_url: str) -> str:
        """Find a same-site pagination link without guessing site-specific APIs."""
        links = re.findall(
            r'<a([^>]*)href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        current_host = urlparse(current_url).netloc.casefold()
        for attributes, href, link_text in links:
            label = re.sub(r'<[^>]+>', '', link_text).strip().casefold()
            aria_label = re.search(r'aria-label=["\']([^"\']+)', attributes, re.IGNORECASE)
            title = re.search(r'title=["\']([^"\']+)', attributes, re.IGNORECASE)
            labels = [label]
            if aria_label:
                labels.append(aria_label.group(1).casefold())
            if title:
                labels.append(title.group(1).casefold())
            if not any(value in {"next", "next page", "next results"} for value in labels):
                continue

            href = unescape(href)
            # Some server-rendered job boards emit `/search-jobs&amp;p=2`.
            # Convert it to a normal query string before resolving the URL.
            if "?" not in href and "&" in href:
                href = href.replace("&", "?", 1)
            next_url = urljoin(current_url, href)
            if urlparse(next_url).netloc.casefold() == current_host:
                return self._merge_query_params(current_url, next_url)
        return ""

    @staticmethod
    def _merge_query_params(current_url: str, next_url: str) -> str:
        """Keep search filters when a pagination link only supplies a page."""
        current = urlparse(current_url)
        target = urlparse(next_url)
        query = dict(parse_qsl(current.query, keep_blank_values=True))
        query.update(parse_qsl(target.query, keep_blank_values=True))
        return urlunparse(target._replace(query=urlencode(query, doseq=True)))

    def _fetch_with_playwright(self) -> List[Job]:
        """Fetch using Playwright for JavaScript-rendered content."""
        jobs = []

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                try:
                    # Career sites often keep analytics or polling requests
                    # open indefinitely. Waiting for network idle turns those
                    # otherwise usable pages into avoidable timeouts.
                    page.goto(self.career_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)

                    # Wait for initial dynamic content to load
                    page.wait_for_timeout(3000)

                    # Scroll/load dynamic content and follow browser-rendered
                    # pagination. Some sites do not expose their Next link in
                    # the initial HTTP response.
                    seen_urls = set()
                    seen_pages = set()
                    for _ in range(self.MAX_BROWSER_PAGES):
                        page_marker = page.url
                        page_jobs = self._scroll_and_extract(page)
                        for job in page_jobs:
                            if job.canonical_url not in seen_urls:
                                seen_urls.add(job.canonical_url)
                                jobs.append(job)

                        marker = (page_marker, tuple(sorted(seen_urls)))
                        if marker in seen_pages:
                            break
                        seen_pages.add(marker)
                        if not self._click_next_page(page):
                            break

                except Exception as e:
                    print(f"Playwright error for {self.company_name}: {e}")
                finally:
                    browser.close()

        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
        except Exception as e:
            print(f"Error with Playwright for {self.company_name}: {e}")

        return jobs

    def _click_next_page(self, page) -> bool:
        """Click an enabled Next control and wait for rendered content."""
        selectors = [
            "a[rel='next']",
            "a[aria-label='Next']",
            "button[aria-label='Next']",
            "a:has-text('Next')",
            "button:has-text('Next')",
        ]
        for selector in selectors:
            try:
                control = page.locator(selector).first
                if control.count() == 0 or not control.is_visible(timeout=300):
                    continue
                if control.get_attribute("aria-disabled") == "true" or not control.is_enabled():
                    continue
                control.click()
                page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue
        return False

    def _scroll_and_extract(self, page) -> List[Job]:
        """Scroll page to load all dynamic content and extract jobs."""
        max_scrolls = 20
        scroll_pause = 1000  # ms
        prev_job_count = 0
        no_change_count = 0

        for i in range(max_scrolls):
            # Try clicking "Load More" or "Show More" buttons
            self._click_load_more(page)

            # Scroll to bottom
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(scroll_pause)

            # Extract jobs from current content
            html = page.content()
            jobs = self._extract_jobs_from_html(html, self.career_url)

            # Check if we found new jobs
            if len(jobs) == prev_job_count:
                no_change_count += 1
                if no_change_count >= 2:
                    # No new jobs after 2 scrolls, stop
                    break
            else:
                no_change_count = 0
                prev_job_count = len(jobs)

        return jobs

    def _click_load_more(self, page):
        """Try to click common 'Load More' buttons."""
        load_more_selectors = [
            "button:has-text('Load More')",
            "button:has-text('Show More')",
            "button:has-text('View More')",
            "button:has-text('See More')",
            "a:has-text('Load More')",
            "a:has-text('Show More')",
            "[data-testid='load-more']",
            ".load-more",
            ".show-more",
        ]

        for selector in load_more_selectors:
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=500):
                    button.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass

    def _extract_jobs_from_html(self, html: str, base_url: str) -> List[Job]:
        """Extract job listings from HTML content."""
        jobs = self._extract_json_ld_jobs(html, base_url)
        seen_urls = {job.canonical_url for job in jobs}

        # Find all links
        link_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
        matches = re.findall(link_pattern, html, re.DOTALL | re.IGNORECASE)

        for href, link_text in matches:
            # Clean up link text
            text = re.sub(r'<[^>]+>', '', link_text).strip()

            # Skip if no meaningful text
            if not text or len(text) < 3:
                continue

            # Skip excluded patterns
            if any(re.search(pat, href, re.IGNORECASE) for pat in self.EXCLUDE_PATTERNS):
                continue

            # Also look for links with job IDs or specific patterns
            job_id_pattern = r'/(\d{5,}|[a-f0-9-]{8,})'
            has_job_id = bool(re.search(job_id_pattern, href))
            parsed_href = urlparse(urljoin(base_url, href))
            path = parsed_href.path.casefold().rstrip("/")
            has_detail_path = bool(re.search(
                r"/(?:jobs?|careers?|positions?|roles?|vacancies|openings|opportunities)/[^/]+",
                path,
            ))
            has_job_query = bool(re.search(
                r"(?:job(?:id|reqid)?|positionid|requisitionid)=",
                parsed_href.query,
                re.IGNORECASE,
            ))

            # Broad words like "Careers" and "Search Jobs" are navigation,
            # not postings. Require a detail-shaped path or identifier so a
            # landing-page link cannot suppress the Playwright fallback.
            if has_job_id or has_detail_path or has_job_query:
                # Make URL absolute
                url = urljoin(base_url, href)

                # Deduplicate
                canonical = url.split("?")[0].lower()
                if canonical in seen_urls:
                    continue
                seen_urls.add(canonical)

                # Basic title cleaning
                title = self._clean_title(text)

                if len(title) > 5:  # Skip very short titles
                    jobs.append(Job(
                        company=self.company_name,
                        title=title,
                        url=url,
                        source="generic",
                    ))

        return jobs

    def _extract_json_ld_jobs(self, page_html: str, base_url: str) -> List[Job]:
        """Parse schema.org JobPosting records embedded by many career sites."""
        jobs = []
        seen_urls = set()
        scripts = re.findall(
            r'''<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>''',
            page_html,
            re.DOTALL | re.IGNORECASE,
        )

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            item_type = value.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "JobPosting" in types:
                title = value.get("title") or value.get("name")
                url = value.get("url")
                if title and url:
                    location = self._json_ld_location(value.get("jobLocation"))
                    job = Job(
                        company=self.company_name,
                        title=str(title),
                        url=urljoin(base_url, str(url)),
                        location=location,
                        source="generic-jsonld",
                    )
                    if job.canonical_url not in seen_urls:
                        seen_urls.add(job.canonical_url)
                        jobs.append(job)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)

        for script in scripts:
            try:
                visit(json.loads(unescape(script).strip()))
            except (json.JSONDecodeError, TypeError):
                continue
        return jobs

    @staticmethod
    def _json_ld_location(value) -> str:
        locations = value if isinstance(value, list) else [value]
        names = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address") or {}
            if not isinstance(address, dict):
                address = {}
            name = ", ".join(
                str(part)
                for part in (
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry"),
                )
                if part
            ) or str(location.get("name") or "")
            if name and name not in names:
                names.append(name)
        return "; ".join(names)

    def _clean_title(self, text: str) -> str:
        """Clean up extracted job title."""
        # Remove extra whitespace
        text = " ".join(text.split())

        # Remove common suffixes
        for suffix in [" - Apply", " - View", " - Learn More", "Apply Now", "View Job"]:
            text = text.replace(suffix, "")

        return text.strip()
