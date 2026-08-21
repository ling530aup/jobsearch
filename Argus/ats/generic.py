"""Generic career page fetcher using Playwright."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from typing import List
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .base import (
    CareerFetcher,
    create_browser_context,
    dismiss_browser_overlays,
    goto_browser_page,
    is_access_challenge_page,
    is_access_challenge_response,
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
        r"/signin", r"/signup", r"^/go/", r"\.pdf$", r"\.png$",
        r"\.jpg$", r"javascript:", r"mailto:", r"tel:"
    ]
    # Some large employers expose thousands of roles as server-rendered pages.
    # Keep the ceiling finite to avoid an accidental pagination loop, while
    # allowing 15-role pages such as Citi's complete catalogue to finish.
    MAX_STATIC_PAGES = 300
    # A few server-rendered job boards (notably Craft/Sprig) expose every
    # numbered page but only ten jobs per response.  A small, per-company
    # pool avoids turning 20 known pages into 20 serial network round trips
    # without materially increasing load on a single host.
    STATIC_PAGE_WORKERS = 3
    # Avature advertises an exact result total and public offset URLs, but
    # many tenants cap every response at ten records despite a larger page
    # size parameter. Keep its finite catalogue fetch bounded per company.
    AVATURE_PAGE_WORKERS = 6
    # Browser pagination is a last resort for unknown boards.  Do not let a
    # blocked or non-job landing page occupy a crawl worker indefinitely.
    # API-specific adapters retain their own complete pagination logic.
    MAX_BROWSER_PAGES = 12
    BROWSER_CRAWL_BUDGET_SECONDS = 75.0
    # A 403/451 entry page can occasionally render in Chromium while refusing
    # httpx, so retain one short discovery attempt.  It must not monopolise a
    # worker for minutes when the browser is blocked as well.
    BLOCKED_ENTRY_BROWSER_BUDGET_SECONDS = 20.0
    MAX_BROWSER_SCROLLS = 6

    def __init__(self, company_name: str, career_url: str, timeout: float = 30.0):
        super().__init__(company_name, career_url, timeout)
        self._playwright = None
        self._browser = None
        self._static_pages_fetched = 0
        self._blocked_by_access_challenge = False
        self._browser_entry_url = career_url
        self._browser_deadline = None
        self.discovered_ats_type = ""
        self.discovered_ats_url = ""

    def fetch_job_list(self) -> List[Job]:
        """Fetch jobs using Playwright for full page rendering."""
        # First try simple HTTP fetch
        jobs = self._fetch_simple()
        if self._blocked_by_access_challenge:
            print(
                f"{self.company_name}: access-verification page detected; "
                "skipping repeated crawl attempts"
            )
            return jobs
        # A branded ATS was found while reading the static landing page.
        # Return immediately so the orchestrator can rerun the dedicated
        # adapter. Starting Playwright here used to hide that discovery behind
        # a slow generic browser loop (notably on branded SuccessFactors).
        if self.discovered_ats_type and self.discovered_ats_url:
            return jobs
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
        page_fallbacks = {}
        page_url = self.career_url
        self._static_pages_fetched = 0
        search_hops = 0

        for _ in range(self.MAX_STATIC_PAGES):
            if self.stop_requested():
                break
            if page_url in seen_pages:
                break
            seen_pages.add(page_url)

            try:
                response = self.client.get(page_url)
            except Exception:
                fallback_url = page_fallbacks.pop(page_url, "")
                if fallback_url and fallback_url not in seen_pages:
                    page_url = fallback_url
                    continue
                break
            if is_access_challenge_response(response):
                self._blocked_by_access_challenge = True
                break
            if response.status_code != 200:
                alternate_url = self._alternate_careers_host_url(
                    page_url,
                    response.status_code,
                    seen_pages,
                )
                if alternate_url:
                    print(
                        f"[{self.company_name}] access denied at corporate "
                        f"host; probing public careers subdomain"
                    )
                    page_url = alternate_url
                    continue
                fallback_url = page_fallbacks.pop(page_url, "")
                if fallback_url and fallback_url not in seen_pages:
                    page_url = fallback_url
                    continue
                break
            if is_access_challenge_page(response.text):
                self._blocked_by_access_challenge = True
                break
            self._static_pages_fetched += 1

            # A corporate landing page can lead to a branded ATS page whose
            # hostname does not reveal the vendor.  Inspect the loaded HTML
            # before trying to generically paginate it: once a supported
            # adapter is identified, the orchestrator will rerun the company
            # through that adapter and use its API-based pagination instead.
            discovered_adapter = self._record_embedded_ats_marker(
                response.text,
                str(response.url),
            )

            page_jobs = self._extract_jobs_from_html(response.text, str(response.url))
            added_on_page = 0
            for job in page_jobs:
                if job.canonical_url not in seen_job_urls:
                    seen_job_urls.add(job.canonical_url)
                    jobs.append(job)
                    added_on_page += 1
            if discovered_adapter:
                break
            if page_jobs and added_on_page == 0:
                break

            # Sprig's server-rendered pager explicitly exposes the final page
            # number.  Fetch that finite set with a deliberately small pool;
            # regular pagination remains serial so sites that rate-limit
            # page navigation keep their existing behaviour.
            parallel_pages = self._sprig_page_urls(
                response.text,
                str(response.url),
                seen_pages,
            )
            if parallel_pages:
                for page_job in self._fetch_static_pages(parallel_pages):
                    if page_job.canonical_url not in seen_job_urls:
                        seen_job_urls.add(page_job.canonical_url)
                        jobs.append(page_job)
                break

            avature_pages = self._avature_page_urls(
                response.text,
                str(response.url),
                len(page_jobs),
                seen_pages,
            )
            if avature_pages:
                print(
                    f"[{self.company_name}] Avature pagination: "
                    f"fetching {len(avature_pages)} remaining pages with "
                    f"{self.AVATURE_PAGE_WORKERS} workers"
                )
                for page_job in self._fetch_static_pages(
                    avature_pages,
                    worker_limit=self.AVATURE_PAGE_WORKERS,
                ):
                    if page_job.canonical_url not in seen_job_urls:
                        seen_job_urls.add(page_job.canonical_url)
                        jobs.append(page_job)
                break

            next_page_url = self._find_next_page_url(response.text, str(response.url))
            preferred_page_url = self._maximize_static_page_size_url(
                response.text,
                str(response.url),
            )
            if not preferred_page_url:
                preferred_page_url = self._avature_page_size_url(
                    response.text,
                    str(response.url),
                )
            if preferred_page_url and preferred_page_url not in seen_pages:
                if next_page_url and next_page_url != preferred_page_url:
                    page_fallbacks[preferred_page_url] = next_page_url
                page_url = preferred_page_url
                continue

            page_url = next_page_url
            if not page_url:
                page_url = self._next_offset_url(str(response.url), len(page_jobs))
            if not page_url:
                page_url = self._next_total_aware_page_url(
                    response.text,
                    str(response.url),
                    len(page_jobs),
                    len(jobs),
                )
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
    def _alternate_careers_host_url(
        page_url: str,
        status_code: int,
        seen_pages: set,
    ) -> str:
        """Try one conventional public careers host after a blocked www host.

        Large employers commonly keep the marketing site on ``www`` and the
        public ATS on ``careers``.  This is deliberately limited to a single
        same-domain request after a definitive access denial; it is not a URL
        guess loop and never changes configured company metadata.
        """
        if status_code not in {403, 451}:
            return ""
        parsed = urlparse(page_url)
        host = parsed.netloc.casefold()
        if not host.startswith("www."):
            return ""
        careers_host = f"careers.{host[4:]}"
        alternate_url = urlunparse(parsed._replace(
            netloc=careers_host,
            path="/",
            query="",
            fragment="",
        ))
        return "" if alternate_url in seen_pages else alternate_url

    def _fetch_static_pages(
        self,
        page_urls: List[str],
        worker_limit: int = None,
    ) -> List[Job]:
        """Fetch a finite, explicitly advertised static page set in parallel."""
        jobs = []
        worker_count = min(worker_limit or self.STATIC_PAGE_WORKERS, len(page_urls))
        if not worker_count:
            return jobs

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self.client.get, page_url): page_url
                for page_url in page_urls
            }
            for future in as_completed(futures):
                try:
                    response = future.result()
                except Exception:
                    continue
                if is_access_challenge_response(response):
                    self._blocked_by_access_challenge = True
                    continue
                if response.status_code != 200:
                    continue
                if is_access_challenge_page(response.text):
                    self._blocked_by_access_challenge = True
                    continue
                self._static_pages_fetched += 1
                jobs.extend(
                    self._extract_jobs_from_html(response.text, str(response.url))
                )
        return jobs

    @staticmethod
    def _avature_page_urls(
        page_html: str,
        current_url: str,
        result_count: int,
        seen_pages: set,
    ) -> List[str]:
        """Return a complete finite page set for public Avature searches."""
        if result_count <= 0 or not re.search(
            r'<meta\b[^>]*\bavature\.', page_html, re.IGNORECASE,
        ):
            return []
        total_match = re.search(
            r'''displaying\s+\d[\d,]*\s*[-–]\s*\d[\d,]*\s+of\s+([\d,]+)\s+results''',
            page_html,
            re.IGNORECASE,
        )
        if not total_match:
            return []
        total = int(total_match.group(1).replace(",", ""))
        if total <= result_count:
            return []
        current = urlparse(current_url)
        candidate_match = re.search(
            r'''href=["']([^"']*\bjobOffset=\d+[^"']*)["']''',
            page_html,
            re.IGNORECASE,
        )
        if not candidate_match:
            return []
        template = urlparse(urljoin(current_url, unescape(candidate_match.group(1))))
        same_search_endpoint = (
            template.netloc.casefold() == current.netloc.casefold()
            and template.path.rstrip("/") == current.path.rstrip("/")
        )
        # An Avature portal often serves its initial catalogue from
        # ``/.../careers`` but puts its public result offsets under its
        # ``/.../careers/SearchJobs`` child. This is the only cross-path
        # transition accepted here; arbitrary offset links remain rejected.
        avature_search_child = (
            template.netloc.casefold() == current.netloc.casefold()
            and template.path.rstrip("/").casefold()
            == f"{current.path.rstrip('/').casefold()}/searchjobs"
        )
        if not same_search_endpoint and not avature_search_child:
            return []
        query = dict(parse_qsl(template.query, keep_blank_values=True))
        offset_key = next(
            (key for key in query if key.casefold() == "joboffset"), ""
        )
        if not offset_key:
            return []
        urls = []
        for offset in range(result_count, total, result_count):
            page_query = dict(query)
            page_query[offset_key] = str(offset)
            page_url = urlunparse(template._replace(
                query=urlencode(page_query, doseq=True),
            ))
            if page_url not in seen_pages:
                urls.append(page_url)
        return urls

    @staticmethod
    def _sprig_page_urls(
        page_html: str,
        current_url: str,
        seen_pages: set,
    ) -> List[str]:
        """Return all remaining pages from a public Craft/Sprig pager.

        This deliberately requires both Sprig's public fragment endpoint and
        accessible ``Go to page N`` links.  It therefore cannot turn an
        arbitrary ``page`` query parameter into concurrent traffic.
        """
        if "sprig-core/components/render" not in page_html.casefold():
            return []
        matches = re.findall(
            r'''<a\b[^>]*href=["']([^"']+)["'][^>]*aria-label=["']Go to page\s+(\d+)["']''',
            page_html,
            re.IGNORECASE,
        )
        if not matches:
            return []

        parsed_current = urlparse(current_url)
        current_query = dict(parse_qsl(parsed_current.query, keep_blank_values=True))
        current_page = int(current_query.get("page", "1") or "1")
        max_page = max(int(page_number) for _, page_number in matches)
        if max_page <= current_page or max_page > GenericFetcher.MAX_STATIC_PAGES:
            return []

        template_url = urljoin(current_url, unescape(matches[0][0]))
        template = urlparse(template_url)
        if (
            template.netloc.casefold() != parsed_current.netloc.casefold()
            or template.path.rstrip("/") != parsed_current.path.rstrip("/")
        ):
            return []
        query = dict(parse_qsl(template.query, keep_blank_values=True))
        page_key = next((key for key in query if key.casefold() == "page"), "")
        if not page_key:
            return []

        urls = []
        for page_number in range(current_page + 1, max_page + 1):
            page_query = dict(query)
            page_query[page_key] = str(page_number)
            page_url = urlunparse(template._replace(query=urlencode(page_query, doseq=True)))
            if page_url not in seen_pages:
                urls.append(page_url)
        return urls

    @staticmethod
    def _maximize_static_page_size_url(page_html: str, current_url: str) -> str:
        """Use an advertised larger server-rendered page size when available.

        Avature, Taleo-like portals, and several white-label career sites
        expose a small default page through ``jobRecordsPerPage`` or similar
        parameters even though their own form/pagination advertises a 100-row
        option.  Raising only those explicit server-side controls reduces
        hundreds of sequential requests without increasing crawler workers.
        """
        size_keys = {
            "jobrecordsperpage", "folderrecordsperpage", "pagesize", "page_size",
        }
        candidates = re.findall(
            r'''(?:href|action|data-url)\s*=\s*["']([^"']+)["']''',
            page_html,
            re.IGNORECASE,
        )
        current_host = urlparse(current_url).netloc.casefold()
        current_path = urlparse(current_url).path.rstrip("/")
        for raw_candidate in candidates:
            # ``urljoin(current_url, "#main")`` inherits the current query.
            # Require an explicit page-size parameter in the raw link so a
            # same-page anchor cannot masquerade as a page-size control.
            raw_query = dict(parse_qsl(
                urlparse(unescape(raw_candidate)).query,
                keep_blank_values=True,
            ))
            if not any(key.casefold() in size_keys for key in raw_query):
                continue
            candidate = urljoin(current_url, unescape(raw_candidate))
            parsed = urlparse(candidate)
            # A feed URL can expose the same pagination parameter while
            # returning RSS/XML rather than job cards. Only follow a large
            # page-size URL for this exact search endpoint.
            if (
                parsed.netloc.casefold() != current_host
                or parsed.path.rstrip("/") != current_path
            ):
                continue
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            size_key = next((key for key in query if key.casefold() in size_keys), "")
            if not size_key or not str(query[size_key]).isdigit():
                continue
            current_size = int(query[size_key])
            # Do not manufacture a larger value from a normal pagination
            # link. Some portals advertise a 100-row preference but cap the
            # rendered result at 9; only their explicit 100-row URL is safe.
            if current_size < 100:
                continue
            for key in list(query):
                if key.casefold() in {"joboffset", "folderoffset", "offset", "start"}:
                    query[key] = "0"
            optimized = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
            if optimized != current_url:
                return optimized
        return ""

    @staticmethod
    def _avature_page_size_url(page_html: str, current_url: str) -> str:
        """Use Avature's documented server-side result window when present.

        Avature search pages often link only to the 10-row default even when
        the same public endpoint accepts ``jobRecordsPerPage=100``.  The
        vendor metadata plus the existing pagination parameter make this a
        narrow platform rule, rather than guessing a page size for arbitrary
        sites.
        """
        if not re.search(r'<meta\b[^>]*\bavature\.', page_html, re.IGNORECASE):
            return ""
        parsed = urlparse(current_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        size_key = next(
            (key for key in query if key.casefold() == "jobrecordsperpage"),
            "",
        )
        if not size_key or not str(query[size_key]).isdigit():
            return ""
        if not 0 < int(query[size_key]) < 100:
            return ""
        query[size_key] = "100"
        for key in list(query):
            if key.casefold() == "joboffset":
                query[key] = "0"
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _next_total_aware_page_url(
        page_html: str,
        current_url: str,
        page_job_count: int,
        total_job_count: int,
    ) -> str:
        """Advance simple ``?page=N`` boards whose pager is client-rendered.

        Public boards often expose a reliable result total in their HTML but
        make the numbered controls buttons instead of links.  Use that total
        only when the document also signals pagination; this avoids guessing
        query parameters for an ordinary careers landing page.
        """
        if page_job_count <= 0 or total_job_count <= 0:
            return ""
        totals = re.findall(
            r'''(?:showing|results?)\s+\d[\d,]*\s*(?:to|[-–])\s*\d[\d,]*\s+of\s+([\d,]+)''',
            page_html,
            re.IGNORECASE,
        )
        totals.extend(re.findall(
            r'''\b([\d,]+)\s+(?:matching\s+)?results\b''',
            page_html,
            re.IGNORECASE,
        ))
        # ``[\d,]+`` intentionally accepts formatted totals such as
        # ``1,814``.  On malformed or partially rendered pages it can also
        # match punctuation-only text (for example ``,``), which must not
        # abort the whole company crawl.
        numeric_totals = (
            value.replace(",", "")
            for value in totals
        )
        total = max(
            (int(value) for value in numeric_totals if value.isdigit()),
            default=0,
        )
        if total <= total_job_count:
            return ""
        if not re.search(
            r'''(?:pagination|pager|page\s*\d+|go\s+to\s+page|results\s+per\s+page)''',
            page_html,
            re.IGNORECASE,
        ):
            return ""

        parsed = urlparse(current_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        page_key = next(
            (
                key for key in query
                if key.casefold() in {"page", "p", "pg", "pagenumber", "page_number"}
            ),
            "page",
        )
        raw_current = query.get(page_key, "1")
        if not str(raw_current).isdigit():
            return ""
        query[page_key] = str(int(raw_current) + 1)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _next_offset_url(current_url: str, result_count: int) -> str:
        """Advance common offset pagination when the page has no Next anchor."""
        parsed = urlparse(current_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        folded = {key.casefold(): key for key, _ in pairs}
        offset_name = next(
            (
                folded[key]
                for key in (
                    "start", "offset", "from", "joboffset", "folderoffset",
                )
                if key in folded
            ),
            "",
        )
        size_name = next(
            (
                folded[key]
                for key in (
                    "rows", "limit", "pagesize", "page_size", "size",
                    "jobrecordsperpage", "folderrecordsperpage",
                )
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
            r'<(a|form|button)([^>]*)>(.*?)</\1>',
            page_html,
            re.DOTALL | re.IGNORECASE,
        ):
            attribute = {
                "a": "href",
                "form": "action",
                "button": "data-href",
            }[tag.casefold()]
            url_match = re.search(
                r'''(?:href|action|data-href|data-url|data-link-url|data-target)\s*=\s*["']([^"']+)["']''',
                attributes,
                re.IGNORECASE,
            )
            if not url_match:
                # A number of corporate career pages use a button with an
                # inline redirect instead of an anchor. Extract only literal
                # HTTP/path targets; never execute arbitrary page JavaScript.
                url_match = re.search(
                    r'''(?:location(?:\.href)?\s*=\s*|window\.open\s*\(\s*)["']([^"']+)["']''',
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
                "explore opportunities", "explore all jobs", "find opportunities",
                "find open jobs", "find open roles", "view job openings",
                "find open positions",
                "find open jobs", "find open roles", "view job openings",
                "find open positions", "open roles", "view roles", "vacancies",
            )
            has_search_phrase = any(phrase in searchable for phrase in phrases)
            has_search_path = bool(re.search(
                r"(?:^|/)(?:search(?:-and-apply)?(?:[-/]|$)|job-search|search-jobs|"
                r"searchjobs|jobsearch|search-results?|search-result-page|"
                r"tgnewui/search|search/home|externaljobs/searchjobs|"
                r"open-positions?|all-jobs?|jobs)"
                r"(?:/|$|[?&#])",
                parsed_url.path.casefold(),
            ))
            if not has_search_phrase and not has_search_path:
                continue
            if any(part in searchable for part in ("login", "sign in", "profile")):
                continue
            # Prefer explicit search/catalogue paths over generic career links.
            score = sum(
                phrase in searchable
                for phrase in (
                    "search jobs", "job search", "find jobs", "all jobs", "open positions",
                    "explore all jobs", "view job openings", "find open jobs",
                    "find open roles", "find open positions",
                )
            )
            if has_search_path:
                score += 2
            if any(term in searchable for term in ("student", "campus", "early career")):
                score -= 4
            candidates.append((score, url))
        best_url = max(candidates, default=(0, ""), key=lambda item: item[0])[1]
        if not best_url:
            return ""
        parsed = urlparse(best_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key in list(query):
            if key.casefold() in {
                "start", "offset", "from", "joboffset", "folderoffset",
            } and str(query[key]).isdigit():
                query[key] = "0"
            if key.casefold() in {
                "rows", "limit", "pagesize", "page_size", "size",
                "jobrecordsperpage", "folderrecordsperpage",
            }:
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
        offset_keys = {"offset", "start", "from", "joboffset"}
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
                    folded_key in offset_keys
                    and value > current_value
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
        # Some server-rendered boards (notably the current Uber board) serve
        # ``/jobs/`` but emit pagination links as ``/jobs?page=2``.  The
        # slashless variant can be challenged or routed differently even
        # though it represents the same catalogue.
        if current.path.endswith("/") and target.path.rstrip("/") == current.path.rstrip("/"):
            target = target._replace(path=current.path)
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
                    browser_budget = self.BROWSER_CRAWL_BUDGET_SECONDS
                    client = self._client
                    if (
                        client is not None
                        and getattr(client, "last_status_code", None) in {403, 451}
                    ):
                        browser_budget = self.BLOCKED_ENTRY_BROWSER_BUDGET_SECONDS
                        print(
                            f"[{self.company_name}] HTTP access denied; "
                            f"limiting browser discovery to {browser_budget:.0f}s"
                        )
                    self._browser_deadline = perf_counter() + browser_budget
                    # Career sites often keep analytics or polling requests
                    # open indefinitely. Waiting for network idle turns those
                    # otherwise usable pages into avoidable timeouts.
                    self._goto_with_retries(page, self._browser_entry_url)

                    # Wait for initial dynamic content to load
                    page.wait_for_timeout(3000)
                    if is_access_challenge_page(page.content()):
                        self._blocked_by_access_challenge = True
                        print(
                            f"{self.company_name}: browser reached an "
                            "access-verification page; skipping pagination"
                        )
                        return jobs
                    dismiss_browser_overlays(page)
                    # A landing-page CTA triggers a new route/request and
                    # needs a short settle period.  When no CTA exists, the
                    # preceding initial render wait is already sufficient;
                    # avoid adding a fixed delay to every generic crawl.
                    if self._click_job_search_cta(page):
                        page.wait_for_timeout(1_000)

                    seen_urls = set()
                    seen_pages = set()
                    seen_job_sets = set()
                    for landing_hop in range(2):
                        # Scroll/load dynamic content and follow rendered
                        # pagination. Some landing pages need one extra hop to
                        # their Search jobs catalogue first.
                        for _ in range(self.MAX_BROWSER_PAGES):
                            if self.stop_requested() or self._browser_budget_expired():
                                break
                            page_marker = page.url
                            page_jobs = self._scroll_and_extract(page)
                            self._enrich_jobs_from_rendered_cards(page, page_jobs)
                            self._record_ats_candidates_from_html(
                                page.content(),
                                page.url,
                            )
                            for job in page_jobs:
                                if job.canonical_url not in seen_urls:
                                    seen_urls.add(job.canonical_url)
                                    jobs.append(job)

                            # A page with no result cards after the bounded
                            # scroll pass is not evidence that its paginator
                            # is a job paginator.  Following it was the cause
                            # of 10--30 minute loops on 403-protected sites.
                            # Let the one permitted landing-page search hop
                            # run below, but never enumerate empty pages.
                            if not page_jobs and not api_jobs:
                                break

                            # Some protection/interstitial pages leave a
                            # visually enabled paginator behind.  A changing
                            # client-side URL is not evidence of progress if
                            # the same result cards are shown again.
                            job_set = frozenset(
                                job.canonical_url for job in page_jobs
                            )
                            if job_set:
                                if job_set in seen_job_sets:
                                    break
                                seen_job_sets.add(job_set)
                            marker = (page_marker, tuple(sorted(seen_urls)))
                            if marker in seen_pages:
                                break
                            seen_pages.add(marker)
                            if not self._click_next_page(page):
                                break

                        if (
                            jobs
                            or api_jobs
                            or landing_hop
                            or self._browser_budget_expired()
                        ):
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
                    self._browser_deadline = None
                    context.close()
                    browser.close()

        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install")
        except Exception as e:
            print(f"Error with Playwright for {self.company_name}: {e}")

        return jobs

    def _browser_budget_expired(self) -> bool:
        return (
            self._browser_deadline is not None
            and perf_counter() >= self._browser_deadline
        )

    @staticmethod
    def _click_job_search_cta(page) -> bool:
        """Open a jobs catalogue when a landing page exposes only a CTA.

        This is intentionally label- and role-based. It handles common
        landing pages without storing a company-specific catalogue URL in
        YAML, while avoiding broad clicks such as ``Apply`` or ``Sign in``.
        """
        labels = re.compile(
            r"(?:see|view|explore|search|find|browse|show|open).{0,30}"
            r"(?:jobs?|roles?|positions?|opportunities)",
            re.IGNORECASE,
        )
        for selector in ("button", "[role='button']"):
            try:
                control = page.locator(selector).filter(has_text=labels).first
                if control.count() and control.is_visible(timeout=300):
                    control.click(timeout=1_500)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _enrich_jobs_from_rendered_cards(page, jobs: List[Job]) -> None:
        """Read locations from the nearest rendered result card for each link."""
        if not jobs:
            return
        try:
            rendered = page.evaluate(
                r"""
                () => Array.from(document.querySelectorAll('a[href]')).map(anchor => {
                    const card = anchor.closest([
                        'li', 'article', '[data-job-id]', '[data-testid*="job"]',
                        '[class*="job-card"]', '[class*="job_card"]',
                        '[class*="job-item"]', '[class*="job_item"]',
                        '[class*="job-result"]', '[class*="job_result"]',
                        '[class*="search-result"]', '[class*="search_result"]',
                        '[class*="list-item"]', '[class*="list_item"]',
                        '[class*="posting"]', '[class*="position"]',
                        '[data-qa*="job"]', '[data-testid*="posting"]'
                    ].join(','));
                    if (!card) return null;
                    const locationNode = card.querySelector([
                        '[itemprop="jobLocation"]', '[data-location]',
                        '[data-location-name]', '[data-qa*="location"]',
                        '[data-testid*="location"]', '[aria-label*="location" i]',
                        '[class*="location"]'
                    ].join(','));
                    let location = locationNode
                        ? (locationNode.dataset.location || locationNode.dataset.locationName || locationNode.textContent || '').trim()
                        : '';
                    // Avature and white-label boards sometimes render a
                    // literal label without a location class.
                    if (!location) {
                        const match = (card.textContent || '').match(
                            /(?:locations?|office)\s*:\s*([^\n|]{2,160})/i
                        );
                        location = match ? match[1].trim() : '';
                    }
                    return location ? {url: anchor.href, location} : null;
                }).filter(Boolean)
                """
            )
        except Exception:
            return

        jobs_by_url = {job.canonical_url: job for job in jobs}
        for item in rendered or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            canonical_url = Job(
                company="",
                title="",
                url=str(item["url"]),
            ).canonical_url
            job = jobs_by_url.get(canonical_url)
            location = " ".join(str(item.get("location") or "").split())
            if job and location and not job.location:
                job.location = location

    def _goto_with_retries(self, page, url: str) -> None:
        """Navigate with bounded retries for browser-level connection resets."""
        last_error = None
        for attempt in range(2):
            if self._browser_budget_expired():
                raise RuntimeError("generic browser crawl time budget exhausted")
            try:
                remaining = self.timeout
                if self._browser_deadline is not None:
                    remaining = min(
                        remaining,
                        max(1.0, self._browser_deadline - perf_counter()),
                    )
                goto_browser_page(page, url, remaining)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 1:
                    page.wait_for_timeout(500 * (attempt + 1))
        raise last_error

    def _record_ats_candidates_from_html(self, page_html: str, base_url: str) -> None:
        """Remember a public ATS board exposed only after JavaScript rendering."""
        from .detector import ATSDetector

        if self._record_embedded_ats_marker(page_html, base_url):
            return

        self._record_ats_candidate(base_url)
        for candidate in ATSDetector._extract_candidate_urls(page_html, base_url):
            self._record_ats_candidate(candidate)

    def _record_embedded_ats_marker(self, page_html: str, base_url: str) -> bool:
        """Record an ATS identified by unambiguous embedded board metadata."""
        # Modern Eightfold Candidate Experience pages are commonly served on
        # a company-owned domain, so their URL is intentionally vendor-free.
        # ``pcsx-data`` / ``smartApplyData`` is the authoritative public
        # marker and lets us switch from slow UI pagination to Eightfold's
        # search API as soon as the linked careers page is loaded.
        if re.search(
            r'''<code[^>]+id=["'](?:smartApplyData|pcsx-data)["']''',
            page_html,
            re.IGNORECASE,
        ):
            self.discovered_ats_type = "eightfold"
            self.discovered_ats_url = base_url
            return True

        # SAP SuccessFactors can be branded on an employer-owned domain.
        # Its static ``/go/Search-Jobs/`` pager belongs in the dedicated
        # adapter, not the generic browser fallback.
        page_text = page_html.casefold()
        if (
            "rmkcdn.successfactors.com" in page_text
            or "j2w.tc.init" in page_text
            or "platform/js/search/search.js" in page_text
        ):
            self.discovered_ats_type = "successfactors"
            self.discovered_ats_url = base_url
            return True

        # Company-owned SSR boards can expose Workday public job URLs in
        # their initial state. Use the CXS adapter instead of rendering every
        # UI page, but only after validating the extracted board URL.
        from .detector import ATSDetector
        for candidate in ATSDetector._extract_candidate_urls(page_html, base_url):
            if (
                ATSDetector._check_url_patterns(candidate) == "workday"
                and ATSDetector.is_public_board_url(candidate, "workday")
            ):
                self.discovered_ats_type = "workday"
                self.discovered_ats_url = ATSDetector._normalize_ats_url(
                    candidate, "workday",
                )
                return True
        return False

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
        title_keys = ("title", "jobTitle", "job_title", "posting_name", "jobName", "text")
        title_keys += (
            "positionTitle", "position_title", "requisitionTitle",
            "postingTitle", "vacancyTitle",
        )
        url_keys = (
            "url", "jobUrl", "job_url", "applyUrl", "apply_url",
            "applyURL", "originalUrl", "originalURL",
            "absolute_url", "externalPath", "canonicalPositionUrl",
            "careers_url", "detailUrl", "detail_url",
            "jobDetailUrl", "job_detail_url", "positionUrl",
            "postingUrl", "vacancyUrl", "jobPath", "job_path",
            "detailPageUrl", "postingPath", "positionPath",
            "hostedUrl", "hosted_url",
        )
        id_keys = (
            "jobId", "job_id", "requisitionId", "requisition_id",
            "requisitionID", "requisitionNumber", "postingId", "positionId", "reqId",
            "id",
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
                (
                    "fullLocation", "formattedAddress", "locationName",
                    "location_name", "locationText", "location_text",
                    "displayLocation", "display_location", "displayName",
                    "location", "name",
                ),
            )
            if direct:
                return str(direct)
            for key in (
                "workLocation", "work_location", "workplace", "jobLocation",
                "job_location", "address", "addressData", "address_data",
                "place", "geography",
            ):
                nested = format_location(value.get(key))
                if nested:
                    return nested
            return ", ".join(
                str(value.get(key))
                for key in (
                    "city", "cityName", "city_name", "state", "stateName",
                    "region", "regionName", "country", "countryName",
                    "country_name", "countryCode", "country_code",
                )
                if value.get(key)
            )

        def job_location(item) -> str:
            """Read both nested and flat public job-location fields.

            Search APIs do not consistently wrap city/country in a
            ``location`` object.  Falling back to the record itself keeps
            country filtering correct for those feeds without guessing from
            the title or URL.
            """
            for key in (
                "location", "locations", "jobLocation", "primaryLocation",
                "locationData", "workLocation", "workplace", "locationText",
                "displayLocation", "address", "addresses", "office", "offices",
                "categories",
            ):
                location = format_location(item.get(key))
                if location:
                    return location
            return format_location(item)

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
                location = job_location(value)
                team = first_value(
                    value,
                    ("department", "team", "jobFunction", "jobCategory", "category"),
                )
                if not team and isinstance(value.get("categories"), dict):
                    team = first_value(
                        value["categories"],
                        ("department", "team", "commitment"),
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
                # Text such as "Next" also appears in carousels, cookie
                # banners and unrelated site navigation.  Following those
                # controls can create a long loop with the same one or two
                # apparent job links.  A rel=next link is semantic pagination;
                # every other broad selector must be inside a pager.
                if control.get_attribute("rel") != "next":
                    in_pager = control.evaluate(
                        "element => Boolean(element.closest("
                        "'nav, [role=navigation], [class*=pagination i], [class*=pager i]'))"
                    )
                    if not in_pager:
                        continue
                control.click()
                page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue

        # Some modern career pages intentionally expose no literal “Next”
        # control. Their paginator is a group of buttons labelled “Go to page
        # 2” (or just “2”) and is common on headless CMS and white-label ATS
        # sites. Select only the immediate next page inside pagination-like
        # controls, never an arbitrary numeric button elsewhere in the page.
        try:
            current = urlparse(page.url)
            query = dict(parse_qsl(current.query, keep_blank_values=True))
            current_page = next(
                (int(value) for key, value in query.items()
                 if key.casefold() in {"page", "p", "pg", "pagenumber", "page_number"}
                 and str(value).isdigit()),
                1,
            )
            controls = page.locator("a, button, [role='button']")
            for index in range(controls.count()):
                control = controls.nth(index)
                try:
                    if not control.is_visible(timeout=250) or not control.is_enabled():
                        continue
                    attributes = " ".join(filter(None, (
                        control.get_attribute("aria-label"),
                        control.get_attribute("title"),
                        control.get_attribute("href"),
                    )))
                    text = " ".join((control.inner_text(timeout=250) or "").split())
                    label = f"{attributes} {text}".casefold()
                    target = None
                    href = control.get_attribute("href") or ""
                    if href:
                        target_query = dict(parse_qsl(
                            urlparse(urljoin(page.url, href)).query,
                            keep_blank_values=True,
                        ))
                        target = next(
                            (int(value) for key, value in target_query.items()
                             if key.casefold() in {"page", "p", "pg", "pagenumber", "page_number"}
                             and str(value).isdigit()),
                            None,
                        )
                    if target is None:
                        number_match = re.search(
                            r"(?:go\s+to\s+)?page\s*(\d+)|^\s*(\d+)\s*$",
                            label,
                        )
                        if number_match:
                            target = int(number_match.group(1) or number_match.group(2))
                    if target != current_page + 1:
                        continue
                    in_pager = control.evaluate(
                        "element => Boolean(element.closest("
                        "'nav, [role=navigation], [class*=pagination i], [class*=pager i]'))"
                    )
                    if not in_pager:
                        continue
                    control.click(timeout=1_500)
                    page.wait_for_timeout(1_500)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        # Some catalogues expose only numbered links. Follow the immediate
        # next numeric page while preserving the board's own query parameters.
        try:
            current = urlparse(page.url)
            query = dict(parse_qsl(current.query, keep_blank_values=True))
            current_page = next(
                (int(value) for key, value in query.items()
                 if key.casefold() in {"page", "p", "pg", "pagenumber"}
                 and str(value).isdigit()),
                1,
            )
            anchors = page.locator("a[href]")
            for index in range(anchors.count()):
                anchor = anchors.nth(index)
                href = anchor.get_attribute("href") or ""
                target = urlparse(urljoin(page.url, href))
                target_query = dict(parse_qsl(target.query, keep_blank_values=True))
                target_page = next(
                    (int(value) for key, value in target_query.items()
                     if key.casefold() in {"page", "p", "pg", "pagenumber"}
                     and str(value).isdigit()),
                    None,
                )
                if target_page != current_page + 1:
                    continue
                if not anchor.is_visible(timeout=300) or not anchor.is_enabled():
                    continue
                anchor.click()
                page.wait_for_timeout(1_500)
                return True
        except Exception:
            pass
        return False

    def _scroll_and_extract(self, page) -> List[Job]:
        """Scroll page to load all dynamic content and extract jobs."""
        max_scrolls = self.MAX_BROWSER_SCROLLS
        scroll_pause = 1000  # ms
        prev_job_count = 0
        no_change_count = 0

        for i in range(max_scrolls):
            if self.stop_requested() or self._browser_budget_expired():
                break
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
            "button:has-text('More Results')",
            "a:has-text('Load More')",
            "a:has-text('Show More')",
            "a:has-text('More Results')",
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
        for job in self._extract_embedded_jobs(html, base_url):
            if job.canonical_url not in {item.canonical_url for item in jobs}:
                jobs.append(job)
        seen_urls = {job.canonical_url for job in jobs}

        # Find all links
        link_pattern = (
            r'<a[^>]+(?:href|ph-href|data-href|data-url|data-job-url|data-redirect-url)='
            r'["\']([^"\']+)["\']'
            r'[^>]*>(.*?)</a>'
        )
        matches = re.findall(link_pattern, html, re.DOTALL | re.IGNORECASE)

        for href, link_text in matches:
            if href.strip().startswith("#"):
                continue
            # Clean up link text
            text = self._job_title_from_link_html(link_text)

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

        self._enrich_job_locations_from_cards(html, base_url, jobs)
        return jobs

    @staticmethod
    def _job_title_from_link_html(link_html: str) -> str:
        """Extract a title from a job-card link without inheriting card copy.

        Some career sites wrap the whole card in a single anchor. Its raw
        text then includes date, location, team, and description, whereas an
        inner heading is the actual posting title. Prefer that semantic
        heading and retain the raw text fallback for conventional links.
        """
        heading = re.search(
            r'''<h[1-4]\b[^>]*>(.*?)</h[1-4]>''',
            link_html,
            re.DOTALL | re.IGNORECASE,
        )
        source = heading.group(1) if heading else link_html
        return re.sub(r'<[^>]+>', '', source).strip()

    def _extract_embedded_jobs(self, page_html: str, base_url: str) -> List[Job]:
        """Read job cards embedded in JSON props used by React/SSR boards.

        Several modern boards render only the first batch as HTML and put the
        card metadata in a JSON hydration payload. Parsing that payload gives
        us the authoritative title, location, and detail URL while the
        browser fallback can still click the board's own load-more control.
        """
        jobs = []
        decoder = json.JSONDecoder()

        def format_location(value) -> str:
            """Render Avature/SSR location values consistently for filtering."""
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                values = [format_location(item) for item in value]
                return "; ".join(dict.fromkeys(item for item in values if item))
            if not isinstance(value, dict):
                return ""
            for key in (
                "fullLocation", "formattedAddress", "locationName",
                "locationText", "displayLocation", "displayName", "name",
            ):
                if value.get(key):
                    return str(value[key])
            for key in (
                "workLocation", "workplace", "jobLocation", "address",
                "addressData", "place", "geography",
            ):
                nested = format_location(value.get(key))
                if nested:
                    return nested
            return ", ".join(
                str(value[key])
                for key in ("city", "state", "region", "country", "countryName")
                if value.get(key)
            )

        # Optimizely/Angular pages commonly put the complete first result
        # batch in an HTML attribute (for example ``data-list-json``) rather
        # than in a normal script tag.  Decode the attribute first so escaped
        # quotes and non-ASCII job titles are restored correctly.
        payloads = []
        for match in re.finditer(
            r'''\bdata-list-json\s*=\s*["']([^"']+)["']''',
            page_html,
            re.IGNORECASE,
        ):
            try:
                payloads.append(json.loads(unescape(match.group(1))))
            except (TypeError, ValueError):
                continue

        # React/SSR boards use a JSON object containing an ``items`` array.
        # Keep the raw decoder because the surrounding HTML is not itself
        # valid JSON.
        for match in re.finditer(r'"items"\s*:\s*\[', page_html):
            try:
                payload, _ = decoder.raw_decode(page_html[match.end() - 1:])
            except (TypeError, ValueError):
                continue
            payloads.append(payload)

        # Next.js and similar SSR frameworks keep the initial catalogue in a
        # JSON script instead of exposing it as a normal HTML attribute. Use
        # the same recursive parser as browser responses so the first page is
        # not lost when JavaScript is delayed or blocked.
        for script in re.findall(
            r'''<script[^>]+(?:id=["']__NEXT_DATA__["']|type=["']application/json["'])[^>]*>(.*?)</script>''',
            page_html,
            re.DOTALL | re.IGNORECASE,
        ):
            try:
                script_payload = json.loads(unescape(script))
                # The useful array is often nested several levels below
                # props/pageProps. Reuse the recursive response parser rather
                # than assuming a particular framework schema.
                for job in self._extract_jobs_from_json(script_payload, base_url):
                    if job.canonical_url not in {item.canonical_url for item in jobs}:
                        jobs.append(job)
            except (TypeError, ValueError):
                continue

        # Some SSR career sites initialize their client-side job store with a
        # JavaScript assignment rather than a JSON script element.  Reading
        # this public state avoids discarding structured locations simply
        # because cards are hydrated after the page has loaded.
        for match in re.finditer(
            r"window\.(?:__PRELOAD(?:ED)?_STATE__|__INITIAL_STATE__)\s*=\s*",
            page_html,
            re.IGNORECASE,
        ):
            try:
                state, _ = decoder.raw_decode(page_html[match.end():])
                for job in self._extract_jobs_from_json(state, base_url):
                    if job.canonical_url not in {item.canonical_url for item in jobs}:
                        jobs.append(job)
            except (TypeError, ValueError):
                continue

        for payload in payloads:
            if isinstance(payload, dict):
                items = next(
                    (
                        payload[key]
                        for key in ("items", "leverListItems", "jobs", "results")
                        if isinstance(payload.get(key), list)
                    ),
                    [],
                )
            else:
                items = payload
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("name")
                href = (
                    item.get("href")
                    or item.get("url")
                    or item.get("jobUrl")
                    or item.get("externalPath")
                )
                if not title or not href:
                    continue
                url = urljoin(base_url, str(href))
                job = Job(
                    company=self.company_name,
                    title=self._clean_title(str(title)),
                    url=url,
                    location=format_location(
                        item.get("location")
                        or item.get("locationName")
                        or item.get("locationsText")
                        or item.get("locations")
                        or item.get("jobLocation")
                        or item.get("workLocation")
                        or item.get("workplace")
                        or item.get("locationText")
                        or item.get("displayLocation")
                        or item.get("address")
                        or item.get("office")
                        or item
                    ).strip(),
                    source="generic",
                )
                if job.canonical_url not in {existing.canonical_url for existing in jobs}:
                    jobs.append(job)
        return jobs

    @staticmethod
    def _enrich_job_locations_from_cards(
        page_html: str,
        base_url: str,
        jobs: List[Job],
    ) -> None:
        """Associate card-level location text with already parsed job links.

        Many server-rendered boards put the title URL and location in sibling
        elements. Link-only extraction used to retain the job while silently
        dropping its location, causing every result to fail later filtering.
        ``li`` and ``article`` cover common ATS result cards without guessing
        across unrelated sections of the page.
        """
        jobs_by_url = {job.canonical_url: job for job in jobs}
        if not jobs_by_url:
            return

        cards = re.findall(
            r"<(?:li|article)\b[^>]*>(.*?)</(?:li|article)>",
            page_html,
            re.DOTALL | re.IGNORECASE,
        )
        for card in cards:
            location_match = re.search(
                r'''<[^>]+(?:class|data-testid|itemprop)=["'][^"']*location[^"']*["'][^>]*>(.*?)</[^>]+>''',
                card,
                re.DOTALL | re.IGNORECASE,
            )
            if location_match:
                location = unescape(re.sub(r"<[^>]+>", " ", location_match.group(1)))
            else:
                attribute_match = re.search(
                    r'''\bdata-location=["']([^"']+)["']''',
                    card,
                    re.IGNORECASE,
                )
                location = unescape(attribute_match.group(1)) if attribute_match else ""
                if not location:
                    icon_match = re.search(
                        r'''alt=["'][^"']*location[^"']*["'][^>]*>.*?'''
                        r'''<p\b[^>]*>(.*?)</p>''',
                        card,
                        re.DOTALL | re.IGNORECASE,
                    )
                    if icon_match:
                        location = unescape(
                            re.sub(r"<[^>]+>", " ", icon_match.group(1))
                        )
                if not location:
                    # Avature and a few white-label boards render a literal
                    # ``Location:`` label without a location class. Keep the
                    # extraction inside the current card so the next result
                    # cannot donate its location.
                    label_match = re.search(
                        r"(?:locations?|office)\s*:\s*(?:<[^>]+>\s*)*"
                        r"([^<]{2,160})",
                        card,
                        re.IGNORECASE,
                    )
                    if label_match:
                        location = label_match.group(1)
            location = " ".join(location.split())
            if not location:
                continue

            for href in re.findall(
                r'''<a\b[^>]+href=["']([^"']+)["']''',
                card,
                re.IGNORECASE,
            ):
                canonical_url = Job(
                    company="",
                    title="",
                    url=urljoin(base_url, unescape(href)),
                ).canonical_url
                job = jobs_by_url.get(canonical_url)
                if job and not job.location:
                    job.location = location

        # Div-based result cards cannot be parsed safely with a nested-div
        # regular expression. Associate a known job link only with location
        # markup that follows it before the next link. This covers TalentBrew
        # layouts such as Barclays while keeping the match bounded to the
        # current result card instead of scanning unrelated page locations.
        links = list(re.finditer(
            r'''<a\b[^>]+href=["']([^"']+)["'][^>]*>''',
            page_html,
            re.IGNORECASE,
        ))
        for index, link in enumerate(links):
            canonical_url = Job(
                company="",
                title="",
                url=urljoin(base_url, unescape(link.group(1))),
            ).canonical_url
            job = jobs_by_url.get(canonical_url)
            if not job or job.location:
                continue

            # Component frameworks often carry a card's searchable metadata
            # in an inline click handler instead of rendering a dedicated
            # location node. Restrict this to the opening tag of a known job
            # link, so navigation/analytics metadata elsewhere cannot supply
            # a location to an unrelated result.
            link_metadata = unescape(link.group(0))
            inline_location = re.search(
                r'''(?:job[_-]?location|location(?:name|text)?)\s*["']?\s*:\s*["']([^"']+)''',
                link_metadata,
                re.IGNORECASE,
            )
            if inline_location:
                location = " ".join(unescape(inline_location.group(1)).split())
                if location:
                    job.location = location
                    continue

            fragment_end = (
                min(links[index + 1].start(), link.end() + 5_000)
                if index + 1 < len(links)
                else min(len(page_html), link.end() + 5_000)
            )
            fragment = page_html[link.end():fragment_end]
            location_match = re.search(
                r'''<[^>]+(?:class|data-testid|itemprop)=["'][^"']*location[^"']*["'][^>]*>(.*?)</[^>]+>''',
                fragment,
                re.DOTALL | re.IGNORECASE,
            )
            if not location_match:
                continue
            location = unescape(
                re.sub(r"<[^>]+>", " ", location_match.group(1))
            )
            location = " ".join(location.split())
            if location:
                job.location = location

        # Some server-rendered boards use div/section result cards rather
        # than li/article. The browser path above handles these when JS is
        # available; this bounded fallback keeps static HTTP extraction from
        # losing locations when the browser is unavailable or delayed.
        for match in re.finditer(
            r'''<(?:div|section)\b[^>]*(?:class|data-testid|data-qa)=["'][^"']*'''
            r'''(?:job|result|posting|position|list-item|list_item)[^"']*["'][^>]*>''',
            page_html,
            re.IGNORECASE,
        ):
            start = match.start()
            next_card = re.search(
                r'''<(?:div|section)\b[^>]*(?:class|data-testid|data-qa)=["'][^"']*'''
                r'''(?:job|result|posting|position|list-item|list_item)[^"']*["'][^>]*>''',
                page_html[match.end():],
                re.IGNORECASE,
            )
            end = match.end() + next_card.start() if next_card else min(len(page_html), match.end() + 12000)
            card = page_html[start:end]
            location_match = re.search(
                r'''(?:locations?|office)\s*:\s*(?:<[^>]+>\s*)*([^<|\n]{2,160})''',
                card,
                re.IGNORECASE,
            )
            if not location_match:
                location_match = re.search(
                    r'''<[^>]+(?:class|data-testid|itemprop)=["'][^"']*location[^"']*["'][^>]*>(.*?)</[^>]+>''',
                    card,
                    re.DOTALL | re.IGNORECASE,
                )
            if not location_match:
                continue
            location = unescape(re.sub(r"<[^>]+>", " ", location_match.group(1)))
            location = " ".join(location.split())
            if not location:
                continue
            for href in re.findall(
                r'''<a\b[^>]+(?:href|ph-href|data-href|data-url|data-job-url|data-redirect-url)=["']([^"']+)["']''',
                card,
                re.IGNORECASE,
            ):
                canonical_url = Job(company="", title="", url=urljoin(base_url, unescape(href))).canonical_url
                job = jobs_by_url.get(canonical_url)
                if job and not job.location:
                    job.location = location

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
        text = " ".join(unescape(text).split())

        # Remove common suffixes
        for suffix in [" - Apply", " - View", " - Learn More", "Apply Now", "View Job"]:
            text = text.replace(suffix, "")

        return text.strip()
