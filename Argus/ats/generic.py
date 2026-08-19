"""Generic career page fetcher using Playwright."""

import json
import re
from typing import List
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    install_browser_page_handlers,
    scroll_page_to_bottom,
)
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
        self._static_pages_fetched = 0
        self._browser_entry_url = career_url
        self.discovered_ats_type = ""
        self.discovered_ats_url = ""

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs using Playwright for full page rendering."""
        # First try simple HTTP fetch
        jobs = self._fetch_simple()
        # Multiple server-rendered pages were followed to exhaustion, so this
        # is normally complete. A single page with some jobs is ambiguous:
        # many JS boards render only their first 10-25 results in HTML.
        if jobs and self._static_pages_fetched > 1:
            return jobs

        browser_jobs = self._fetch_with_playwright()
        seen_urls = {job.canonical_url for job in jobs}
        for job in browser_jobs:
            if job.canonical_url not in seen_urls:
                seen_urls.add(job.canonical_url)
                jobs.append(job)
        return jobs

    def _fetch_simple(self) -> List[Job]:
        """Fetch static job pages, following same-site next-page links."""
        jobs = []
        seen_job_urls = set()
        seen_pages = set()
        page_url = self.career_url
        self._static_pages_fetched = 0
        search_hops = 0

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
            self._static_pages_fetched += 1

            page_jobs = self._extract_jobs_from_html(response.text, str(response.url))
            added_on_page = 0
            for job in page_jobs:
                if job.canonical_url not in seen_job_urls:
                    seen_job_urls.add(job.canonical_url)
                    jobs.append(job)
                    added_on_page += 1
            if page_jobs and added_on_page == 0:
                break

            page_url = self._find_next_page_url(response.text, str(response.url))
            if not page_url:
                page_url = self._next_offset_url(str(response.url), len(page_jobs))
            if not page_url:
                if not page_jobs and search_hops < 2:
                    search_url = self._find_search_page_url(
                        response.text,
                        str(response.url),
                    )
                    if search_url and search_url not in seen_pages:
                        self._browser_entry_url = search_url
                        page_url = search_url
                        search_hops += 1
                        continue
                break

        return jobs

    @staticmethod
    def _next_offset_url(current_url: str, result_count: int) -> str:
        """Advance common offset pagination when the page has no Next anchor."""
        parsed = urlparse(current_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        folded = {key.casefold(): key for key, _ in pairs}
        offset_name = next(
            (folded[key] for key in ("start", "offset", "from") if key in folded),
            "",
        )
        size_name = next(
            (
                folded[key]
                for key in ("rows", "limit", "pagesize", "page_size", "size")
                if key in folded
            ),
            "",
        )
        if not offset_name or not size_name:
            return ""
        if not str(query[offset_name]).isdigit() or not str(query[size_name]).isdigit():
            return ""
        page_size = int(query[size_name])
        offset = int(query[offset_name])
        rows_is_end_index = (
            size_name.casefold() == "rows"
            and any(key.casefold() in {"search", "ref"} for key in query)
            and page_size > offset
        )
        if rows_is_end_index:
            window_size = page_size - offset
            if window_size <= 0 or result_count < window_size:
                return ""
            query[offset_name] = str(page_size)
            query[size_name] = str(page_size + window_size)
            return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

        if page_size <= 0 or result_count < page_size:
            return ""
        query[offset_name] = str(offset + page_size)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _find_search_page_url(page_html: str, current_url: str) -> str:
        """Find the catalogue link when configured URL is only a careers landing page."""
        candidates = []
        for tag, attributes, inner_html in re.findall(
            r'<(a|form)([^>]*)>(.*?)</\1>',
            page_html,
            re.DOTALL | re.IGNORECASE,
        ):
            attribute = "href" if tag.casefold() == "a" else "action"
            url_match = re.search(
                rf'''{attribute}\s*=\s*["']([^"']+)["']''',
                attributes,
                re.IGNORECASE,
            )
            if not url_match:
                continue
            text = " ".join(re.sub(r"<[^>]+>", " ", inner_html).split()).casefold()
            aria = re.search(r'''aria-label=["']([^"']+)["']''', attributes, re.I)
            if aria:
                text = f"{text} {aria.group(1).casefold()}"
            url = urljoin(current_url, unescape(url_match.group(1)))
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"}:
                continue
            if any(
                marker in parsed_url.path.casefold()
                for marker in ("savedview-", "primary-link-href", "ph-hero-content")
            ):
                continue
            searchable = re.sub(
                r"[^a-z0-9]+",
                " ",
                f"{text} {parsed_url.path.casefold()}",
            ).strip()
            phrases = (
                "search jobs", "job search", "find jobs", "view jobs", "browse jobs",
                "all jobs", "open positions", "job opportunities",
                "career opportunities", "search opportunities",
                "explore opportunities", "vacancies",
            )
            if not any(phrase in searchable for phrase in phrases):
                continue
            if any(part in searchable for part in ("login", "sign in", "profile")):
                continue
            # Prefer explicit search/catalogue paths over generic career links.
            score = sum(
                phrase in searchable
                for phrase in (
                    "search jobs", "job search", "find jobs", "all jobs", "open positions"
                )
            )
            if any(term in searchable for term in ("student", "campus", "early career")):
                score -= 2
            candidates.append((score, url))
        best_url = max(candidates, default=(0, ""), key=lambda item: item[0])[1]
        if not best_url:
            return ""
        parsed = urlparse(best_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in list(query):
            if key.casefold() in {"start", "offset", "from"} and str(query[key]).isdigit():
                query[key] = "0"
            if key.casefold() in {"rows", "limit", "pagesize", "page_size", "size"}:
                value = query[key]
                if str(value).isdigit() and 0 < int(value) < 100:
                    query[key] = "100"
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _find_next_page_url(self, page_html: str, current_url: str) -> str:
        """Find a same-site pagination link without guessing site-specific APIs."""
        links = []
        for attributes, link_text in re.findall(
            r'<a([^>]*)>(.*?)</a>',
            page_html,
            re.DOTALL | re.IGNORECASE,
        ):
            href_match = re.search(
                r'href\s*=\s*["\']([^"\']+)["\']',
                attributes,
                re.IGNORECASE,
            )
            if href_match:
                links.append((attributes, href_match.group(1), link_text))
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
            rel = re.search(r'rel=["\']([^"\']+)', attributes, re.IGNORECASE)
            is_next_rel = bool(rel and "next" in rel.group(1).casefold().split())
            normalized_labels = {
                re.sub(r"[^a-z0-9]+", " ", value).strip()
                for value in labels
            }
            is_next_label = any(
                value in {"next", "next page", "next results", "load next"}
                or value.startswith("next ")
                or value.startswith("go to next")
                for value in normalized_labels
            )
            if not is_next_rel and not is_next_label:
                continue

            href = unescape(href)
            # Some server-rendered job boards emit `/search-jobs&amp;p=2`.
            # Convert it to a normal query string before resolving the URL.
            if "?" not in href and "&" in href:
                href = href.replace("&", "?", 1)
            next_url = urljoin(current_url, href)
            if urlparse(next_url).netloc.casefold() == current_host:
                return self._merge_query_params(current_url, next_url)

        # Some boards expose only numbered controls (for example ``2``) and
        # no link literally named Next. Follow the immediate next page or the
        # smallest greater offset while staying on the same host.
        current = urlparse(current_url)
        current_query = dict(parse_qsl(current.query, keep_blank_values=True))
        page_keys = {"page", "p", "pg", "pagenumber", "page_number"}
        offset_keys = {"offset", "start", "from"}
        numbered_candidates = []
        for _attributes, href, _link_text in links:
            href = unescape(href)
            candidate = urlparse(urljoin(current_url, href))
            if candidate.netloc.casefold() != current_host:
                continue
            query = dict(parse_qsl(candidate.query, keep_blank_values=True))
            for key, raw_value in query.items():
                folded_key = key.casefold()
                if folded_key not in page_keys | offset_keys or not str(raw_value).isdigit():
                    continue
                value = int(raw_value)
                default = 1 if folded_key in page_keys else 0
                current_value = int(current_query.get(key, default)) if str(
                    current_query.get(key, default)
                ).isdigit() else default
                if value == current_value + 1 or (
                    folded_key in offset_keys and value > current_value
                ):
                    numbered_candidates.append((value, candidate.geturl()))
            path_page = re.search(r"/page/(\d+)(?:/|$)", candidate.path, re.I)
            current_path_page = re.search(r"/page/(\d+)(?:/|$)", current.path, re.I)
            if path_page:
                value = int(path_page.group(1))
                current_value = int(current_path_page.group(1)) if current_path_page else 1
                if value == current_value + 1:
                    numbered_candidates.append((value, candidate.geturl()))
        if numbered_candidates:
            return self._merge_query_params(
                current_url,
                min(numbered_candidates, key=lambda item: item[0])[1],
            )
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
                context = create_browser_context(browser)
                page = context.new_page()
                install_browser_page_handlers(page)
                api_jobs = []
                api_seen_urls = set()

                def handle_response(response):
                    self._record_ats_candidate(response.url)
                    content_type = (response.headers.get("content-type") or "").casefold()
                    url_lower = response.url.casefold()
                    if "json" not in content_type and not any(
                        hint in url_lower
                        for hint in ("job", "career", "position", "requisition", "vacan")
                    ):
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    for job in self._extract_jobs_from_json(data, response.url):
                        if job.canonical_url not in api_seen_urls:
                            api_seen_urls.add(job.canonical_url)
                            api_jobs.append(job)

                page.on("response", handle_response)

                try:
                    # Career sites often keep analytics or polling requests
                    # open indefinitely. Waiting for network idle turns those
                    # otherwise usable pages into avoidable timeouts.
                    self._goto_with_retries(page, self._browser_entry_url)

                    # Wait for initial dynamic content to load
                    page.wait_for_timeout(3000)
                    dismiss_browser_overlays(page)

                    seen_urls = set()
                    seen_pages = set()
                    for landing_hop in range(2):
                        # Scroll/load dynamic content and follow rendered
                        # pagination. Some landing pages need one extra hop to
                        # their Search jobs catalogue first.
                        for _ in range(self.MAX_BROWSER_PAGES):
                            page_marker = page.url
                            page_jobs = self._scroll_and_extract(page)
                            self._record_ats_candidates_from_html(
                                page.content(),
                                page.url,
                            )
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

                        if jobs or api_jobs or landing_hop:
                            break
                        search_url = self._find_search_page_url(page.content(), page.url)
                        if not search_url or search_url == page.url:
                            break
                        self._goto_with_retries(page, search_url)
                        page.wait_for_timeout(2_000)
                        dismiss_browser_overlays(page)

                    for job in api_jobs:
                        if job.canonical_url not in seen_urls:
                            seen_urls.add(job.canonical_url)
                            jobs.append(job)

                except Exception as e:
                    print(f"Playwright error for {self.company_name}: {e}")
                finally:
                    context.close()
                    browser.close()

        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
        except Exception as e:
            print(f"Error with Playwright for {self.company_name}: {e}")

        return jobs

    def _goto_with_retries(self, page, url: str) -> None:
        """Navigate with bounded retries for browser-level connection resets."""
        last_error = None
        for attempt in range(2):
            try:
                goto_browser_page(page, url, self.timeout)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 1:
                    page.wait_for_timeout(500 * (attempt + 1))
        raise last_error

    def _record_ats_candidates_from_html(self, page_html: str, base_url: str) -> None:
        """Remember a public ATS board exposed only after JavaScript rendering."""
        from .detector import ATSDetector

        self._record_ats_candidate(base_url)
        for candidate in ATSDetector._extract_candidate_urls(page_html, base_url):
            self._record_ats_candidate(candidate)

    def _record_ats_candidate(self, url: str) -> None:
        """Store a normalized ATS URL discovered in browser traffic."""
        from .detector import ATSDetector

        ats_type = ATSDetector._check_url_patterns(url)
        if not ats_type or not ATSDetector.is_public_board_url(url, ats_type):
            return
        normalized = ATSDetector._normalize_ats_url(url, ats_type)
        if not ATSDetector.is_public_board_url(normalized, ats_type):
            return

        # Prefer a candidate matching the configured page. Otherwise retain
        # the first valid board; response order normally reveals the primary
        # jobs API before analytics and secondary talent-network requests.
        if not self.discovered_ats_url or ats_type == self.discovered_ats_type:
            self.discovered_ats_type = ats_type
            self.discovered_ats_url = normalized

    def _extract_jobs_from_json(self, data, base_url: str) -> List[Job]:
        """Best-effort extraction from public JSON responses loaded by a board."""
        jobs = []
        seen_urls = set()
        title_keys = ("title", "jobTitle", "job_title", "posting_name", "jobName")
        title_keys += (
            "positionTitle", "position_title", "requisitionTitle",
            "postingTitle", "vacancyTitle",
        )
        url_keys = (
            "url", "jobUrl", "job_url", "applyUrl", "apply_url",
            "absolute_url", "externalPath", "canonicalPositionUrl",
            "careers_url", "detailUrl", "detail_url",
            "jobDetailUrl", "job_detail_url", "positionUrl",
            "postingUrl", "vacancyUrl",
        )
        id_keys = (
            "jobId", "job_id", "requisitionId", "requisition_id",
            "requisitionNumber", "postingId", "positionId", "reqId",
        )

        def first_value(item, keys):
            for key in keys:
                value = item.get(key)
                if value not in (None, ""):
                    return value
            return None

        def format_location(value) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                values = [format_location(item) for item in value]
                return "; ".join(dict.fromkeys(item for item in values if item))
            if not isinstance(value, dict):
                return ""
            direct = first_value(
                value,
                ("fullLocation", "formattedAddress", "locationName", "name"),
            )
            if direct:
                return str(direct)
            return ", ".join(
                str(value.get(key))
                for key in ("city", "state", "region", "country", "countryName")
                if value.get(key)
            )

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            title = first_value(value, title_keys)
            raw_url = first_value(value, url_keys)
            identifier = first_value(value, id_keys)
            if not title and identifier:
                title = value.get("name")
            if title and raw_url and (identifier or self._looks_like_job_url(str(raw_url))):
                url = urljoin(base_url, str(raw_url))
                location = format_location(
                    first_value(value, ("location", "locations", "primaryLocation", "locationData"))
                )
                team = first_value(
                    value,
                    ("department", "team", "jobFunction", "jobCategory", "category"),
                )
                if isinstance(team, dict):
                    team = first_value(team, ("name", "label", "title")) or ""
                job = Job(
                    company=self.company_name,
                    title=str(title),
                    url=url,
                    location=location,
                    team=str(team or ""),
                    source="generic-json",
                )
                if job.canonical_url not in seen_urls:
                    seen_urls.add(job.canonical_url)
                    jobs.append(job)

            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)

        visit(data)
        return jobs

    @staticmethod
    def _looks_like_job_url(url: str) -> bool:
        parsed = urlparse(url)
        searchable = f"{parsed.path}?{parsed.query}".casefold()
        return bool(re.search(
            r"(?:^|[/_?&=-])(?:jobs?|positions?|requisitions?|vacancies|openings)(?:[/_?&=-]|$)",
            searchable,
        ))

    def _click_next_page(self, page) -> bool:
        """Click an enabled Next control and wait for rendered content."""
        selectors = [
            "a[rel='next']",
            "a[aria-label*='Next' i]",
            "button[aria-label*='Next' i]",
            "a[title*='Next' i]",
            "button[title*='Next' i]",
            "[data-testid*='next' i]",
            ".pagination-next",
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
            dismiss_browser_overlays(page)
            # Try clicking "Load More" or "Show More" buttons
            self._click_load_more(page)

            # Scroll to bottom
            if not scroll_page_to_bottom(page):
                break
            page.wait_for_timeout(scroll_pause)

            # Extract jobs from current content
            html = page.content()
            jobs = self._extract_jobs_from_html(html, page.url)

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
            "button:has-text('More Jobs')",
            "button:has-text('Show More Jobs')",
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
            path_and_fragment = f"{path}/{parsed_href.fragment.casefold().lstrip('/')}"
            has_detail_path = bool(re.search(
                r"/(?:jobs?|positions?|roles?|vacanc(?:y|ies)|openings?|"
                r"opportunit(?:y|ies)|requisitions?|postings?)/[^/]+",
                path_and_fragment,
            )) or bool(re.search(
                r"/(?:careers?|search-and-apply)/(?:[^/]+/)*"
                r"(?:job-?details?|job|position|role|vacancy|opening|"
                r"opportunity|requisition|posting)/[^/]+",
                path_and_fragment,
            )) or bool(re.search(
                r"/(?:job-?details?|position-?details?|vacancy-?details?)/[^/]+",
                path_and_fragment,
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
                # Basic title cleaning
                title = self._clean_title(text)

                if len(title) > 5:  # Skip very short titles
                    job = Job(
                        company=self.company_name,
                        title=title,
                        url=url,
                        source="generic",
                    )
                    if job.canonical_url in seen_urls:
                        continue
                    seen_urls.add(job.canonical_url)
                    jobs.append(job)

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
