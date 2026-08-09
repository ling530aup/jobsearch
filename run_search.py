#!/usr/bin/env python3
"""Quick runner script for job search with profiles."""

import os
import logging
import sys

# Change to project directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)

# Get profile from command line or use default
profile = sys.argv[1] if len(sys.argv) > 1 else "default"

# Run the search with the specified profile
from pathlib import Path
from Argus.orchestrator import Orchestrator

profile_dir = Path(f"config/profiles/{profile}")
if not profile_dir.exists():
    print(f"Error: Profile '{profile}' not found")
    print("Available profiles:")
    for p in Path("config/profiles").iterdir():
        if p.is_dir():
            print(f"  - {p.name}")
    sys.exit(1)

# Use profile-specific titles, fall back to global companies
titles_file = profile_dir / "titles.yaml"
companies_file = profile_dir / "companies.yaml"
if not companies_file.exists():
    companies_file = Path("config/companies.yaml")

output_dir = f"job_results/{profile}"

print(f"Profile: {profile}")
print(f"Companies: {companies_file}")
print(f"Titles: {titles_file}")
print(f"Output: {output_dir}")
print()

orchestrator = Orchestrator(
    companies_file=str(companies_file),
    titles_file=str(titles_file),
    output_dir=output_dir,
    timeout=30.0,
)

summary = orchestrator.run()
print("\nDone!")
