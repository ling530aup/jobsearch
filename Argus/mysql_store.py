"""Best-effort MySQL persistence for discovered jobs."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse
from typing import Iterable, Optional

import yaml

from .models import Job

try:
    import mysql.connector
except ImportError:  # MySQL is optional; file persistence must still work.
    mysql = None


logger = logging.getLogger(__name__)


class MySQLStore:
    """Persist jobs to MySQL without making the crawler depend on MySQL."""

    DEFAULT_CONFIG_PATH = Path("config/database.yaml")
    FILTER_COLUMNS = {
        "company": "c.name",
        "location": "j.location",
        "date": "DATE(j.discovered_at)",
    }

    def __init__(self, config_path: Optional[str] = None):
        config = self._load_config(Path(config_path) if config_path else self.DEFAULT_CONFIG_PATH)
        self.url = os.getenv("MYSQL_URL", config.get("url", "jdbc:mysql://localhost:3306/"))
        self.host = os.getenv("MYSQL_HOST", config.get("host", "localhost"))
        self.port = int(os.getenv("MYSQL_PORT", config.get("port", 3306)))
        self.user = os.getenv("MYSQL_USER", config.get("user", "root"))
        self.password = os.getenv("MYSQL_PASSWORD", config.get("password", ""))
        self.schema = os.getenv("MYSQL_DATABASE", config.get("schema", "jobsearch"))
        self._lock = RLock()
        self._database_ready = False
        self._disabled = False
        self._warned = False
        self._crawl_run_id: Optional[int] = None

        parsed = urlparse(self.url.replace("jdbc:", "", 1))
        if parsed.hostname:
            self.host = parsed.hostname
        if parsed.port:
            self.port = parsed.port

    @staticmethod
    def _load_config(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return data.get("mysql", data)
        except Exception as exc:
            logger.error("Unable to read MySQL config %s: %s", path, exc)
            return {}

    def _connect(self, database: Optional[str] = None):
        if mysql is None:
            raise RuntimeError("mysql-connector-python is not installed")
        kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "connection_timeout": 5,
        }
        if database:
            kwargs["database"] = database
        return mysql.connector.connect(**kwargs)

    def _ensure_database(self) -> bool:
        """Check that the separately provisioned MySQL database is reachable."""
        if self._database_ready:
            return True
        if self._disabled:
            return False
        with self._lock:
            if self._database_ready:
                return True
            if self._disabled:
                return False
            connection = None
            try:
                connection = self._connect(self.schema)
                connection.close()
                self._database_ready = True
                logger.info("MySQL database available: %s", self.schema)
                return True
            except Exception as exc:
                if connection:
                    connection.close()
                self._disabled = True
                self._log_failure("MySQL database connection failed", exc)
                return False

    def set_crawl_run_id(self, run_id: Optional[int]) -> None:
        """Attach subsequent newly persisted jobs to the active crawl run."""
        with self._lock:
            self._crawl_run_id = run_id

    @staticmethod
    def _canonical_url(url: str) -> str:
        return str(url).lower().rstrip("/")

    @classmethod
    def _url_hash(cls, url: str) -> str:
        return hashlib.sha256(cls._canonical_url(url).encode("utf-8")).hexdigest()

    @staticmethod
    def _to_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)

    def _get_or_create_company(
        self,
        cursor,
        company_name: str,
        career_url: str,
        ats_type: Optional[str],
    ) -> int:
        """Return a company id; an existing company is updated, not duplicated."""
        cursor.execute("SELECT id FROM companies WHERE name=%s", (company_name,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO companies (name, career_url, ats_type) VALUES (%s, %s, %s)",
                (company_name, career_url, ats_type),
            )
            company_id = cursor.lastrowid
        else:
            company_id = row[0]

        cursor.execute(
            """UPDATE companies
               SET career_url=COALESCE(NULLIF(%s, ''), career_url),
                   ats_type=COALESCE(NULLIF(%s, ''), ats_type),
                   last_crawled=CURRENT_TIMESTAMP(6)
               WHERE id=%s""",
            (career_url, ats_type or "", company_id),
        )
        return company_id

    def _upsert_jobs(self, cursor, jobs: list[Job], company_id: int, run_id: int) -> None:
        """Bulk upsert jobs while preserving the frontend's applied flag."""
        values = [
            (
                company_id,
                run_id,
                self._url_hash(job.url),
                job.url,
                job.title,
                job.location,
                job.team,
                job.source,
                self._to_datetime(job.discovered_at),
                bool(job.applied),
            )
            for job in jobs
        ]
        cursor.executemany(
            """INSERT INTO jobs
               (company_id, crawl_run_id, canonical_url_hash, url, title, location, team,
                source, discovered_at, applied)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE title=VALUES(title),
               location=VALUES(location), team=VALUES(team),
               source=VALUES(source), discovered_at=VALUES(discovered_at),
               updated_at=CURRENT_TIMESTAMP(6)""",
            values,
        )

    def save_jobs(self, jobs: Iterable[Job]) -> None:
        """Persist jobs; MySQL failures never interrupt file persistence."""
        jobs = list(jobs)
        if not jobs or not self._ensure_database():
            return
        with self._lock:
            run_id = self._crawl_run_id
        if run_id is None:
            self._log_failure("MySQL job persistence skipped", ValueError("crawl run was not initialized"))
            return
        company_names = {job.company for job in jobs}
        if len(company_names) != 1:
            self._log_failure(
                "MySQL job persistence skipped",
                ValueError("a save batch must contain jobs from one company"),
            )
            return

        connection = None
        try:
            with self._lock:
                connection = self._connect(self.schema)
                cursor = connection.cursor()
                first_job = jobs[0]
                company_id = self._get_or_create_company(
                    cursor,
                    first_job.company,
                    first_job.career_url or "",
                    first_job.ats_type,
                )
                self._upsert_jobs(cursor, jobs, company_id, run_id)
                connection.commit()
                cursor.close()
        except Exception as exc:
            self._log_failure("MySQL job persistence failed", exc)
        finally:
            if connection and connection.is_connected():
                connection.close()

    def start_crawl_run(self, companies_total: int) -> Optional[int]:
        """Create a crawl_runs row only after the database is available."""
        if not self._ensure_database():
            return None
        connection = None
        try:
            connection = self._connect(self.schema)
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO crawl_runs (started_at, status, companies_total) "
                "VALUES (CURRENT_TIMESTAMP(6), %s, %s)",
                ("running", companies_total),
            )
            run_id = cursor.lastrowid
            connection.commit()
            cursor.close()
            return run_id
        except Exception as exc:
            self._log_failure("MySQL crawl run initialization failed", exc)
            return None
        finally:
            if connection and connection.is_connected():
                connection.close()

    def finish_crawl_run(
        self,
        run_id: Optional[int],
        *,
        status: str,
        companies_succeeded: int,
        companies_failed: int,
        jobs_fetched: int,
        jobs_saved: int,
    ) -> None:
        """Update the run row with the final crawl counters."""
        if run_id is None:
            return
        connection = None
        try:
            connection = self._connect(self.schema)
            cursor = connection.cursor()
            cursor.execute(
                """UPDATE crawl_runs
                   SET finished_at=CURRENT_TIMESTAMP(6), status=%s,
                       companies_succeeded=%s, companies_failed=%s,
                       jobs_fetched=%s, jobs_saved=%s
                   WHERE id=%s""",
                (status, companies_succeeded, companies_failed, jobs_fetched, jobs_saved, run_id),
            )
            connection.commit()
            cursor.close()
        except Exception as exc:
            self._log_failure("MySQL crawl run finalization failed", exc)
        finally:
            if connection and connection.is_connected():
                connection.close()

    @staticmethod
    def _selected_values(value) -> list:
        """Normalize a single filter value and query-string lists alike."""
        if not value:
            return []
        return [value] if isinstance(value, str) else list(value)

    def _resolve_run_id(self, filters: dict) -> tuple[Optional[int], bool]:
        """Return the effective run id and whether the result must be empty."""
        run_id = filters.get("run_id")
        if filters.get("scope") == "latest" and not run_id:
            run_id = self.latest_run_id()
            return run_id, run_id is None
        return run_id, False

    def _build_filter(
        self,
        filters: dict,
        *,
        exclude: Optional[str] = None,
    ) -> tuple[str, list]:
        """Build the shared job-list WHERE clause and its bound parameters."""
        conditions = []
        params = []
        run_id, _ = self._resolve_run_id(filters)
        if run_id:
            conditions.append("j.crawl_run_id=%s")
            params.append(run_id)

        for key, column in self.FILTER_COLUMNS.items():
            if key == exclude:
                continue
            values = self._selected_values(filters.get(key))
            if values:
                placeholders = ", ".join(["%s"] * len(values))
                conditions.append(f"{column} IN ({placeholders})")
                params.extend(values)

        # Whitespace-separated terms are ANDed so "software engineer" matches
        # titles containing both words.
        for term in str(filters.get("title") or "").split():
            conditions.append("j.title LIKE %s")
            params.append(f"%{term}%")
        if filters.get("applied") is not None:
            conditions.append("j.applied=%s")
            params.append(filters["applied"])

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return where, params

    @staticmethod
    def _add_cursor_filter(where: str, params: list, cursor: Optional[dict]) -> tuple[str, list]:
        """Add the keyset-pagination condition without mutating base params."""
        if not cursor:
            return where, params
        cursor_clause = "(j.discovered_at < %s OR (j.discovered_at = %s AND j.id < %s))"
        where = where + (" AND " if where else " WHERE ") + cursor_clause
        return where, params + [cursor["discovered_at"], cursor["discovered_at"], cursor["id"]]

    @staticmethod
    def _page_limit(filters: dict) -> int:
        return min(max(int(filters.get("limit", 50)), 1), 200)

    @staticmethod
    def _next_cursor(rows: list[dict], limit: int) -> Optional[dict]:
        if len(rows) <= limit:
            return None
        last = rows[limit - 1]
        return {"discovered_at": last["discovered_at"].isoformat(), "id": last["id"]}

    def list_jobs(self, filters: dict) -> dict:
        """Return a cursor-paginated global job list for the local dashboard."""
        if not self._ensure_database():
            raise RuntimeError("MySQL is unavailable")
        run_id, empty = self._resolve_run_id(filters)
        if empty:
            return {"jobs": [], "next_cursor": None, "total": 0}

        query_filters = {**filters, "run_id": run_id, "scope": "all"}
        where, params = self._build_filter(query_filters)
        page_where, page_params = self._add_cursor_filter(where, params, filters.get("cursor"))
        limit = self._page_limit(filters)
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "SELECT COUNT(*) AS total FROM jobs j JOIN companies c ON c.id=j.company_id" + where,
                tuple(params),
            )
            total = cursor.fetchone()["total"]
            cursor.execute(
                """SELECT j.id, j.title, j.url, j.location, j.team, j.source,
                          j.discovered_at, j.applied, j.crawl_run_id, c.name AS company
                   FROM jobs j JOIN companies c ON c.id=j.company_id""" + page_where +
                " ORDER BY j.discovered_at DESC, j.id DESC LIMIT %s",
                tuple(page_params + [limit + 1]),
            )
            all_rows = cursor.fetchall()
            cursor.close()
            rows = all_rows[:limit]
            return {"jobs": rows, "next_cursor": self._next_cursor(all_rows, limit), "total": total}
        finally:
            connection.close()

    def latest_run_id(self) -> Optional[int]:
        if not self._ensure_database():
            return None
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT id FROM crawl_runs WHERE jobs_saved > 0 ORDER BY started_at DESC, id DESC LIMIT 1")
            row = cursor.fetchone(); cursor.close()
            return row[0] if row else None
        finally:
            connection.close()

    def list_facets(self, filters: dict, exclude: str) -> dict:
        """Return company, location, and date values for the other filters."""
        if not self._ensure_database():
            raise RuntimeError("MySQL is unavailable")
        run_id, empty = self._resolve_run_id(filters)
        if empty:
            return {"companies": [], "locations": [], "dates": []}
        query_filters = {**filters, "run_id": run_id, "scope": "all"}
        where, params = self._build_filter(query_filters, exclude=exclude)
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor()
            companies = self._fetch_facet_values(cursor, "c.name", where, params)
            location_where = where + (" AND " if where else " WHERE ") + "j.location IS NOT NULL AND j.location <> ''"
            locations = self._fetch_facet_values(cursor, "j.location", location_where, params)
            dates = self._fetch_facet_values(cursor, "DATE(j.discovered_at)", where, params)
            cursor.close()
            return {"companies": companies, "locations": locations, "dates": dates}
        finally:
            connection.close()

    @staticmethod
    def _fetch_facet_values(cursor, column: str, where: str, params: list) -> list:
        """Fetch sorted, distinct non-null values for one facet."""
        cursor.execute(
            f"SELECT DISTINCT {column} FROM jobs j JOIN companies c ON c.id=j.company_id"
            f"{where} ORDER BY {column}",
            tuple(params),
        )
        return [row[0] for row in cursor.fetchall() if row[0] is not None]

    def list_runs(self, limit: int = 30) -> list[dict]:
        if not self._ensure_database():
            raise RuntimeError("MySQL is unavailable")
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """SELECT id, started_at, finished_at, status, companies_total,
                          companies_succeeded, companies_failed, jobs_fetched, jobs_saved, error_message
                   FROM crawl_runs ORDER BY started_at DESC, id DESC LIMIT %s""", (min(max(limit, 1), 100),)
            )
            rows = cursor.fetchall(); cursor.close(); return rows
        finally:
            connection.close()

    def get_run(self, run_id: int) -> Optional[dict]:
        if not self._ensure_database():
            raise RuntimeError("MySQL is unavailable")
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """SELECT id, started_at, finished_at, status, companies_total,
                          companies_succeeded, companies_failed, jobs_fetched, jobs_saved, error_message
                   FROM crawl_runs WHERE id=%s""", (run_id,)
            )
            row = cursor.fetchone(); cursor.close(); return row
        finally:
            connection.close()

    def update_applied(self, job_id: int, applied: bool) -> Optional[dict]:
        if not self._ensure_database():
            raise RuntimeError("MySQL is unavailable")
        connection = self._connect(self.schema)
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("UPDATE jobs SET applied=%s WHERE id=%s", (applied, job_id))
            connection.commit()
            cursor.execute("SELECT id, applied FROM jobs WHERE id=%s", (job_id,))
            row = cursor.fetchone(); cursor.close(); return row
        finally:
            connection.close()

    def _log_failure(self, message: str, exc: Exception) -> None:
        if not self._warned:
            logger.error("%s: %s; continuing with file persistence", message, exc)
            self._warned = True
