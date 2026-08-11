"""Dashboard application services independent from HTTP transport."""

from Argus.mysql_store import MySQLStore

from .query import encode_cursor, first_value, parse_job_filters
from .profile_catalog import ProfileCatalog
from .run_manager import RunManager


class JobService:
    """Database-backed job reads and application-state updates."""

    def __init__(self, database: MySQLStore, profiles: ProfileCatalog):
        self.database = database
        self.profiles = profiles

    def profile_names(self) -> list[str]:
        return self.profiles.names()

    def bootstrap(self) -> dict:
        payload = self.database.list_jobs({"scope": "latest", "limit": 50})
        return {
            "jobs": payload["jobs"],
            "next_cursor": encode_cursor(payload["next_cursor"]),
            "total": payload["total"],
            "profiles": self.profile_names(),
        }

    def list_jobs(self, query: dict) -> dict:
        payload = self.database.list_jobs(parse_job_filters(query, paginated=True))
        payload["next_cursor"] = encode_cursor(payload["next_cursor"])
        return payload

    def list_facets(self, query: dict) -> dict:
        exclude = first_value(query, "exclude", "")
        if exclude not in ("company", "location", "date"):
            raise ValueError("Invalid facet")
        return self.database.list_facets(parse_job_filters(query), exclude)

    def update_applied(self, job_id: int, applied: bool):
        return self.database.update_applied(job_id, applied)


class RunService:
    """Run trigger and persisted/live run status access."""

    def __init__(self, database: MySQLStore, profiles: ProfileCatalog, manager: RunManager):
        self.database = database
        self.profiles = profiles
        self.manager = manager

    def start(self, profile: str) -> dict:
        if not self.profiles.contains(profile):
            raise ValueError(f"Unknown profile: {profile}")
        return self.manager.start(profile)

    def list_runs(self, limit: int):
        limit = min(max(limit, 1), 100)
        rows = self.database.list_runs(min(limit + 1, 100))
        return {"runs": [row for row in rows if row.get("status") != "running"][:limit]}

    def active(self):
        return self.manager.active()

    def get(self, run_id: str):
        live_run = self.manager.get(run_id)
        if live_run:
            return live_run
        if run_id.isdigit():
            return self.database.get_run(int(run_id))
        return None
