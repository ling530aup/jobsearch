#!/usr/bin/env python3
"""Application entry point for the local Argus dashboard."""

import os
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Argus.mysql_store import MySQLStore

from web.http_handler import DashboardHandler
from web.profile_catalog import ProfileCatalog
from web.run_manager import RunManager
from web.services import JobService, RunService


FRONTEND = ROOT / "frontend"
PROFILES = ROOT / "config" / "profiles"


def create_server(port: Optional[int] = None):
    """Build the dashboard with explicit service dependencies."""
    database = MySQLStore()
    profiles = ProfileCatalog(PROFILES)
    jobs = JobService(database, profiles)
    runs = RunService(database, profiles, RunManager(ROOT, PROFILES))
    handler = partial(DashboardHandler, jobs, runs, FRONTEND)
    server_port = port if port is not None else int(os.environ.get("ARGUS_PORT", "8787"))
    return ThreadingHTTPServer(("127.0.0.1", server_port), handler)


def main():
    os.chdir(ROOT)
    server = create_server()
    print(f"Argus dashboard: http://127.0.0.1:{server.server_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
