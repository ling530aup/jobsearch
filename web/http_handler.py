"""HTTP adapter for the dashboard services."""

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .query import integer_value, serialise


class DashboardHandler(BaseHTTPRequestHandler):
    """Translate HTTP requests into calls to the dashboard services."""

    def __init__(self, jobs, runs, frontend: Path, *args, **kwargs):
        self.jobs = jobs
        self.runs = runs
        self.frontend = frontend
        super().__init__(*args, **kwargs)

    def log_message(self, *_):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=serialise).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Invalid JSON")

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/bootstrap":
                payload = self.jobs.bootstrap()
                payload["runs"] = self.runs.list_runs(12)["runs"]
                payload["active_run"] = self.runs.active()
                self.send_json(payload)
            elif parsed.path == "/api/jobs":
                self.send_json(self.jobs.list_jobs(parse_qs(parsed.query)))
            elif parsed.path == "/api/facets":
                self.send_json(self.jobs.list_facets(parse_qs(parsed.query)))
            elif parsed.path == "/api/runs":
                query = parse_qs(parsed.query)
                self.send_json(self.runs.list_runs(integer_value(query, "limit", 30)))
            elif parsed.path.startswith("/api/runs/"):
                self._send_run(parsed.path.rsplit("/", 1)[-1])
            else:
                self.serve_static(parsed.path)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            logging.getLogger(__name__).exception("Dashboard request failed")
            self.send_json({"error": f"Database request failed: {exc}"}, 503)

    def do_POST(self):
        if self.path != "/api/runs":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            profile = self.read_json().get("profile", "default")
            self.send_json(self.runs.start(profile), 202)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 409)

    def do_PATCH(self):
        if not self.path.startswith("/api/jobs/") or not self.path.endswith("/applied"):
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            job_id = int(self.path.split("/")[3])
            payload = self.read_json()
            if not isinstance(payload.get("applied"), bool):
                raise ValueError("applied must be a boolean")
            result = self.jobs.update_applied(job_id, payload["applied"])
            self.send_json(result if result else {"error": "Job not found"}, 200 if result else 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            logging.getLogger(__name__).exception("Applied update failed")
            self.send_json({"error": f"Database update failed: {exc}"}, 503)

    def _send_run(self, run_id: str):
        run = self.runs.get(run_id)
        self.send_json(run if run else {"error": "Run not found"}, 200 if run else 404)

    def serve_static(self, path: str):
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.frontend / relative).resolve()
        if self.frontend not in target.parents and target != self.frontend:
            self.send_json({"error": "Forbidden"}, 403)
            return
        if not target.is_file():
            self.send_json({"error": "Not found"}, 404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
