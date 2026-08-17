"""Company registry with persistence."""

import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from threading import RLock

from .models import Company


class CompanyRegistry:
    """Manages company data with file-based persistence."""

    def __init__(self, registry_path: str = "companies_registry.json"):
        self.registry_path = Path(registry_path)
        self._companies: Dict[str, Company] = {}
        self._lock = RLock()
        self._load()

    def _load(self) -> None:
        """Load registry from file."""
        if self.registry_path.exists():
            with open(self.registry_path, "r") as f:
                data = json.load(f)
                for company_data in data.get("companies", []):
                    company = Company.from_dict(company_data)
                    self._companies[company.name.lower()] = company

    def _save(self) -> None:
        """Save registry to file."""
        data = {
            "companies": [c.to_dict() for c in self._companies.values()],
            "updated_at": datetime.now().isoformat()
        }
        with open(self.registry_path, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, company: Company) -> None:
        """Add or update a company."""
        with self._lock:
            self._companies[company.name.lower()] = company
            self._save()

    def add_many(self, companies: List[Company]) -> None:
        """Add or update companies with one registry write."""
        with self._lock:
            for company in companies:
                self._companies[company.name.lower()] = company
            self._save()

    def get(self, name: str) -> Optional[Company]:
        """Get company by name."""
        return self._companies.get(name.lower())

    def remove(self, name: str) -> bool:
        """Remove a company by name."""
        key = name.lower()
        if key in self._companies:
            del self._companies[key]
            self._save()
            return True
        return False

    def list_all(self) -> List[Company]:
        """Get all companies."""
        return list(self._companies.values())

    def update_ats_type(self, name: str, ats_type: str) -> None:
        """Update the ATS type for a company."""
        with self._lock:
            company = self.get(name)
            if company:
                company.ats_type = ats_type
                self._save()

    def update_detection(self, name: str, ats_type: str, career_url: str) -> None:
        """Persist both the detected ATS and its resolved public board URL."""
        with self._lock:
            company = self.get(name)
            if company:
                company.ats_type = ats_type
                if career_url:
                    company.career_url = career_url
                self._save()

    def update_last_crawled(self, name: str) -> None:
        """Update the last crawled timestamp."""
        with self._lock:
            company = self.get(name)
            if company:
                company.last_crawled = datetime.now().isoformat()
                self._save()
