#!/usr/bin/env python3
"""Job Search Agent - CLI entry point."""

import argparse
import logging
import sys
from pathlib import Path

from Argus.orchestrator import Orchestrator


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Search jobs from target companies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with a profile (recommended)
  python search.py --profile default
  python search.py --profile alice

  # Run with explicit config files
  python search.py -c config/companies.yaml -t config/titles.yaml

  # Longer timeout for slow sites
  python search.py --profile default --timeout 60
        """,
    )

    parser.add_argument(
        "--profile", "-p",
        help="Profile name (loads config from config/profiles/<name>/)",
    )
    parser.add_argument(
        "--companies", "-c",
        help="Path to companies YAML file (overrides profile)",
    )
    parser.add_argument(
        "--titles", "-t",
        help="Path to job titles YAML file (overrides profile)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output directory for results (default: job_results/<profile>)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30)",
    )

    args = parser.parse_args()

    # Resolve config paths
    if args.profile:
        profile_dir = Path(f"config/profiles/{args.profile}")
        if not profile_dir.exists():
            print(f"Error: Profile '{args.profile}' not found at {profile_dir}", file=sys.stderr)
            print(f"Available profiles:", file=sys.stderr)
            profiles_root = Path("config/profiles")
            if profiles_root.exists():
                for p in profiles_root.iterdir():
                    if p.is_dir():
                        print(f"  - {p.name}", file=sys.stderr)
            sys.exit(1)

        # Use profile-specific titles, fall back to global companies
        companies_file = args.companies or str(profile_dir / "companies.yaml")
        if not Path(companies_file).exists():
            companies_file = "config/companies.yaml"

        titles_file = args.titles or str(profile_dir / "titles.yaml")
        if not Path(titles_file).exists():
            print(f"Error: titles.yaml not found in profile '{args.profile}'", file=sys.stderr)
            sys.exit(1)

        output_dir = args.output or f"job_results/{args.profile}"
    else:
        # Require explicit config files if no profile
        if not args.companies or not args.titles:
            print("Error: Either --profile or both --companies and --titles are required", file=sys.stderr)
            parser.print_help()
            sys.exit(1)

        companies_file = args.companies
        titles_file = args.titles
        output_dir = args.output or "job_results"

    try:
        print(f"Profile: {args.profile or 'custom'}")
        print(f"Companies: {companies_file}")
        print(f"Titles: {titles_file}")
        print(f"Output: {output_dir}")
        print()

        orchestrator = Orchestrator(
            companies_file=companies_file,
            titles_file=titles_file,
            output_dir=output_dir,
            timeout=args.timeout,
        )

        summary = orchestrator.run()
        print("\nDone!")

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
