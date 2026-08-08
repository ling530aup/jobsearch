# Argus

A Python-based job search agent that automatically crawls company career pages to find relevant job listings. It supports multiple Applicant Tracking Systems (ATS), user profiles for different preferences, and provides flexible filtering options.

## Features

- **Multi-ATS Support**: Automatically detects and crawls jobs from:
  - Greenhouse
  - Lever
  - Ashby
  - Workday
  - Amazon (custom API)
  - Google (custom fetcher)
  - TikTok (custom API)
  - Uber (custom API)
  - Custom career pages (via Playwright)

- **User Profiles**: Support for multiple users with different job preferences
  - Each profile has its own titles, locations, and filters
  - Results are stored separately per profile
  - Optionally customize company lists per profile

- **Smart Filtering**:
  - Filter by job titles (with fuzzy matching)
  - Filter by location (states, cities, remote)
  - Exclude specific levels (staff, principal, lead, etc.)

- **Auto-Detection**: Automatically detects ATS type from career URLs and finds direct API endpoints

- **Incremental Results**: Saves results organized by date and company, avoiding duplicates across runs

## Installation

```bash
# Clone the repository
git clone https://github.com/mshen1019/Argus.git
cd Argus

# (Optional) Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (for custom ATS sites)
python -m playwright install chromium

# install socksio for httpx for vpn 
pip install "httpx[socks]"
```

## Quick Start

```bash
# Run with the default profile
python run_search.py

# Run with a specific profile
python run_search.py alice
```

This will:
1. Load the profile configuration from `config/profiles/<name>/`
2. Load companies from the profile or fall back to `config/companies.yaml`
3. Crawl all companies and save matching jobs to `job_results/<profile>/`

## User Profiles

Profiles allow multiple users to have different job search preferences. Each profile is a directory under `config/profiles/` containing a `titles.yaml` file.

### Profile Structure

```
config/
├── companies.yaml              # Global company list (shared by all profiles)
└── profiles/
    ├── default/
    │   └── titles.yaml         # Default user preferences
    ├── alice/
    │   ├── titles.yaml         # Alice's job preferences
    │   └── companies.yaml      # (Optional) Alice's custom company list
    └── bob/
        └── titles.yaml         # Bob's job preferences
```

### Creating a New Profile

1. Create a new directory under `config/profiles/`:
   ```bash
   mkdir config/profiles/myprofile
   ```

2. Create a `titles.yaml` with your preferences:
   ```yaml
   titles:
     - Data Scientist
     - Machine Learning Engineer
     - Research Scientist

   locations:
     - California
     - New York
     - Remote

   exclude_levels:
     - junior
     - intern
   ```

3. (Optional) Create a custom `companies.yaml` if you want to search different companies

4. Run your search:
   ```bash
   python run_search.py myprofile
   # or
   python search.py --profile myprofile
   ```

### Profile Output

Results are stored separately for each profile:

```
job_results/
├── default/
│   └── 2026-01-25/
│       ├── OpenAI/
│       │   └── jobs.json
│       └── ...
├── alice/
│   └── 2026-01-25/
│       └── ...
└── bob/
    └── 2026-01-25/
        └── ...
```

## Configuration

### Companies (`config/companies.yaml`)

Define the companies to crawl:

```yaml
companies:
  - name: OpenAI
    career_url: https://jobs.ashbyhq.com/openai
    ats_type: ashby

  - name: Anthropic
    career_url: https://boards.greenhouse.io/anthropic
    ats_type: greenhouse

  - name: Amazon
    career_url: https://www.amazon.jobs
    ats_type: amazon

  - name: Google
    career_url: https://careers.google.com/jobs/results/
    ats_type: google
```

**Supported ATS types:**
- `greenhouse` - Greenhouse.io job boards
- `lever` - Lever.co job boards
- `ashby` - Ashby HQ job boards
- `workday` - Workday job sites
- `amazon` - Amazon.jobs (custom API)
- `google` - Google Careers (custom fetcher)
- `tiktok` - TikTok/ByteDance careers (custom API)
- `uber` - Uber careers (custom API)
- `meta` - Meta careers (limited due to bot detection)
- `custom` - Custom career pages (uses Playwright)

### Job Titles & Filters (`config/profiles/<name>/titles.yaml`)

Configure target job titles, locations, and exclusions:

```yaml
titles:
  - Machine Learning Engineer
  - Senior Machine Learning Engineer
  - Research Scientist
  - Applied Scientist

locations:
  - California
  - Remote

# Exclude specific seniority levels
exclude_levels:
  - staff
  - principal
```

Title and location matching is case-insensitive and profile-driven: configured
values are matched as phrases within the discovered job data, so extra title
or location text is allowed without requiring built-in role or geography lists.
When a configured location is a country (for example, `Canada`), the filter
also matches city-only locations in that country using the local GeoNames city
index. The `geonamescache` dependency must be installed for this country-wide
expansion; explicit city values continue to work without it.

The active profile controls which title configuration is loaded. For example,
`run_search.py custom` loads `config/profiles/custom/titles.yaml`, while
`run_search.py` without an argument loads the `default` profile. Therefore a
title such as `Software Development Engineer` must be present in the profile
being run. Country and city matching is handled by the separate
`Argus/location.py` module and its `CountryLocationMatcher`. When a job
includes an explicit country, that country must also be present in the profile
before the job can pass the location filter. For a city-only location, a city
listed in any configured country is accepted; for example, `Dublin` is accepted
when `Ireland` is configured. This permits valid city-only listings even when
the same city name exists in another country.

The title/location pipeline is:

```text
ATS fetcher -> JobFilter -> LocationFilter/CountryLocationMatcher -> JobStore
```

`JobStore` deduplicates by canonical job URL across the entire output directory.
If a URL was saved on an earlier run, it is not appended to the current date's
file again; inspect the earlier date folder for that job. A matching title can
therefore pass both filters but still have `Saved 0 new jobs` because it is an
existing URL.

#### Title matching logic

`JobFilter` evaluates each discovered job title against every value in the
profile's `titles` list. The matching process is intentionally configuration-
driven and does not depend on a built-in list of roles or industries:

1. Both the configured title and the discovered title are converted to
   case-insensitive normalized text. Punctuation is treated as whitespace and
   repeated whitespace is collapsed.
2. Exclusion levels are checked first. If any configured `exclude_levels` token
   appears as a complete normalized token in the job title, the job is rejected
   immediately.
3. A configured title matches when its normalized phrase appears inside the job
   title. This allows additional seniority, technology, team, or location text,
   for example `Senior Java Software Engineer - Trading` matching
   `Java Software Engineer`.
4. If a configured phrase is not contiguous but all of its tokens are present,
   it is still an exact title-family match. This rule is independent of the
   fuzzy threshold, so `Software Development Engineer, Amazon Foundational
   Security Services` always matches `Software Development Engineer`.
5. Only when not all configured tokens are present does the filter compare the
   configured title with same-sized word windows using `SequenceMatcher`. The
   effective fuzzy threshold is at least `0.80`, allowing minor spelling or
   formatting variations without broadly matching different roles such as
   `Software Engineer` and `Software Developer`.

The job is retained if at least one configured title matches. The best score is
returned internally for diagnostics, but filtering is based on the boolean
match result. To broaden or narrow the search, update the `titles` and
`exclude_levels` values in the profile YAML rather than adding role-specific
rules to the filter code.

**Available exclusion levels:**
- `staff` - Staff-level positions
- `principal` - Principal-level positions
- `lead` - Lead roles
- `director` - Director-level positions
- `manager` - Manager roles
- `head` - Head of department roles
- `vp` - VP-level positions
- `junior` - Junior/Associate positions
- `intern` - Internship positions

## CLI Usage

For more control, use the CLI directly:

```bash
# Using profiles (recommended)
python search.py --profile default
python search.py --profile alice --timeout 60
python run_search.py custom

# Using explicit config files
python search.py \
  --companies config/companies.yaml \
  --titles config/profiles/default/titles.yaml \
  --output job_results/custom
```

**Options:**
- `-p, --profile` - Profile name (loads from `config/profiles/<name>/`)
- `-c, --companies` - Path to companies YAML file (overrides profile)
- `-t, --titles` - Path to job titles YAML file (overrides profile)
- `-o, --output` - Output directory for results
- `--timeout` - Request timeout in seconds (default: 30)

## Output

Results are saved in a profile and date-organized structure:

```
job_results/
└── default/
    └── 2026-01-25/
        ├── OpenAI/
        │   ├── jobs.json
        │   └── jobs.csv
        ├── Anthropic/
        │   ├── jobs.json
        │   └── jobs.csv
        └── ...
```

Each `jobs.json` contains:

```json
[
  {
    "company": "OpenAI",
    "title": "Machine Learning Engineer",
    "url": "https://jobs.ashbyhq.com/openai/abc123",
    "location": "San Francisco, CA",
    "team": "Applied AI",
    "source": "ashby",
    "discovered_at": "2026-01-25T10:30:00"
  }
]
```

## Tools

### Fix ATS Configuration

Automatically detect and fix incorrect ATS types and career URLs:

```bash
python fix_ats_config.py
```

This will:
1. Validate each company's career URL
2. Auto-detect the correct ATS type
3. Find direct ATS URLs when companies use embedded job boards
4. Update `config/companies.yaml` with corrections

### Investigate Unverified Companies

For companies that couldn't be automatically verified:

```bash
python investigate_unverified.py
```

## Project Structure

```
Argus/
├── config/
│   ├── companies.yaml          # Global company list
│   └── profiles/               # User profiles
│       ├── default/
│       │   └── titles.yaml
│       ├── Ming/
│       │   └── titles.yaml
│       └── Yaxi/
│           └── titles.yaml
├── Argus/                      # Main package
│   ├── orchestrator.py         # Main orchestration logic
    ├── filter.py               # Job title filtering and filter composition
    ├── location.py             # Country/city-aware location matching
│   ├── store.py                # Job persistence
│   ├── registry.py             # Company registry management
│   ├── models.py               # Data models
│   └── ats/                    # ATS-specific adapters
│       ├── greenhouse.py
│       ├── lever.py
│       ├── ashby.py
│       ├── workday.py
│       ├── amazon.py           # Amazon.jobs API
│       ├── google.py           # Google Careers
│       ├── tiktok.py           # TikTok/ByteDance
│       ├── uber.py             # Uber Careers
│       ├── generic.py          # Playwright-based fallback
│       └── detector.py         # ATS auto-detection
├── job_results/                # Output directory
│   ├── default/                # Results for default profile
│   └── <profile>/              # Results for other profiles
├── run_search.py               # Quick runner script
├── search.py                   # CLI entry point
├── fix_ats_config.py           # ATS config fixer tool
└── requirements.txt            # Dependencies
```

## Adding New Companies

1. Find the company's career page URL
2. Add to `config/companies.yaml`:

```yaml
  - name: New Company
    career_url: https://jobs.lever.co/newcompany
    ats_type: lever
```

3. If unsure about ATS type, set to `unknown` and run `fix_ats_config.py`

## Supported Companies

The default configuration includes 50+ tech companies:
- AI Labs: OpenAI, Anthropic, DeepMind, Mistral AI, Cohere, xAI, Perplexity AI
- Big Tech: Google, Meta, Apple, Microsoft, Amazon
- Finance: Stripe, Block, Coinbase, Plaid, Brex
- Rideshare: Uber, Lyft
- Social: TikTok, Pinterest, LinkedIn
- And many more...

## Requirements

- Python 3.9+
- httpx
- playwright
- pyyaml

## Usage & Responsibility

This project is intended for **personal job search and small-scale use**.

Users are responsible for ensuring that their use of this tool complies with
the terms of service of the websites they access and with applicable laws
and regulations.

This tool performs **read-only access** to publicly available job postings.
It does **not** automate job applications, form submissions, or authentication
flows.

The author does not operate any centralized crawling service and does not
collect or store user data.

## Contact

For questions, suggestions, or issues, feel free to reach out:
- Email: mshen1019@gmail.com
- GitHub Issues: [https://github.com/mshen1019/Argus/issues](https://github.com/mshen1019/Argus/issues)

## License

MIT
