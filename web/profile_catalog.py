"""Profile discovery for dashboard services."""

from pathlib import Path


class ProfileCatalog:
    """Expose the available crawler profiles from one filesystem boundary."""

    def __init__(self, profiles: Path):
        self.profiles = profiles

    def names(self) -> list[str]:
        if not self.profiles.exists():
            return []
        return sorted(path.name for path in self.profiles.iterdir() if path.is_dir())

    def contains(self, profile: str) -> bool:
        return profile in self.names()
