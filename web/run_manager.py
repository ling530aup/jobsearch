"""Background crawler run management for the dashboard."""

from datetime import datetime
import logging
from pathlib import Path
from threading import RLock, Thread
import uuid

from Argus.orchestrator import Orchestrator


class RunManager:
    """Start at most one crawler run and expose its live in-memory state."""

    MAX_LOGS = 300

    def __init__(self, root: Path, profiles: Path):
        self.root = root
        self.profiles = profiles
        self._lock = RLock()
        self._runs = {}

    def start(self, profile: str) -> dict:
        run_id = str(uuid.uuid4())
        run = {
            "id": run_id,
            "profile": profile,
            "status": "running",
            "progress": 0,
            "total": 0,
            "company": "准备中",
            "logs": [],
            "summary": None,
            "started_at": datetime.now().isoformat(),
        }
        with self._lock:
            if any(item["status"] == "running" for item in self._runs.values()):
                raise RuntimeError("已有搜索正在运行")
            self._runs[run_id] = run

        Thread(
            target=self._run_worker,
            args=(run_id, profile),
            name=f"argus-search-{run_id[:8]}",
            daemon=True,
        ).start()
        return self._snapshot(run_id)

    def get(self, run_id: str):
        with self._lock:
            if run_id not in self._runs:
                return None
            return self._snapshot_locked(run_id)

    def active(self):
        with self._lock:
            for run_id, run in self._runs.items():
                if run["status"] == "running":
                    return self._snapshot_locked(run_id)
        return None

    def _snapshot(self, run_id: str) -> dict:
        with self._lock:
            return self._snapshot_locked(run_id)

    def _snapshot_locked(self, run_id: str) -> dict:
        run = self._runs[run_id]
        return {**run, "logs": list(run["logs"])}

    def _append_log(self, run_id: str, message: str) -> None:
        with self._lock:
            run = self._runs[run_id]
            run["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
            })
            run["logs"] = run["logs"][-self.MAX_LOGS:]

    def _update(self, run_id: str, **values) -> None:
        with self._lock:
            self._runs[run_id].update(values)

    def _run_worker(self, run_id: str, profile: str) -> None:
        class SearchLogHandler(logging.Handler):
            def __init__(self, manager):
                super().__init__()
                self.manager = manager

            def emit(self, record):
                self.manager._append_log(run_id, self.format(record))

        handler = SearchLogHandler(self)
        handler.setFormatter(logging.Formatter("%(levelname)s [%(threadName)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            company_file = self.profiles / profile / "companies.yaml"
            if not company_file.exists():
                company_file = self.root / "config" / "companies.yaml"
            self._append_log(run_id, f"开始运行 profile: {profile}")
            orchestrator = Orchestrator(
                companies_file=str(company_file),
                titles_file=str(self.profiles / profile / "titles.yaml"),
                output_dir=str(self.root / "job_results" / profile),
                timeout=30.0,
                progress_callback=lambda done, total, company, succeeded: self._progress(
                    run_id, done, total, company, succeeded
                ),
            )
            total = len(orchestrator.companies)
            self._update(run_id, total=total)
            summary = orchestrator.run()
            self._update(
                run_id,
                status="completed",
                progress=total,
                summary=summary,
                finished_at=datetime.now().isoformat(),
            )
            self._append_log(run_id, "搜索完成。新职位已写入数据库。")
        except Exception as exc:
            logging.getLogger(__name__).exception("Search failed")
            self._update(
                run_id,
                status="failed",
                error=str(exc),
                finished_at=datetime.now().isoformat(),
            )
            self._append_log(run_id, f"搜索失败: {exc}")
        finally:
            root_logger.removeHandler(handler)

    def _progress(self, run_id: str, done: int, total: int, company: str, succeeded: bool) -> None:
        self._update(run_id, progress=done, total=total, company=company)
        self._append_log(run_id, f"{'✓' if succeeded else '×'} {company} 完成 ({done}/{total})")
