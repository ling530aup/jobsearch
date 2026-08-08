# Argus – Crawler-Based Design Doc (Updated)

## 1. Overview
This document describes the updated design of a **shareable, crawler-based Argus** that supports **both LLM and non-LLM modes**. The system is designed so that users **without any LLM access** can still use core job discovery features, while users **with LLM access** can enable enhanced matching, normalization, and intelligence.

The agent can be safely shared with other users, providing a clear capability boundary depending on whether LLM credentials are configured.

This document describes the updated design of a **crawler-based Argus**. The system automatically crawls career websites for a predefined list of companies, searches for user-specified job titles, and stores discovered job postings (metadata + links) into a structured folder. A **separate Job Application Agent** will later consume these stored jobs to handle applications.

**Core principles**:
- Crawler-first (headless, deterministic, schedulable)
- Company-centric (persistent memory of career URLs)
- Title-driven search
- Clear separation between *job discovery* and *job application*

---

## 2. Goals & Non-Goals

### Goals
- Be safely shareable and usable by others (open-source / internal tool)
- Support **two execution modes**:
  - **Non-LLM mode** (default, zero LLM dependency)
  - **LLM-enhanced mode** (optional, opt-in)
- Allow users without LLM access to:
  - Crawl company career sites
  - Search by job title
  - Persist job postings and links
- Allow users with LLM access to:
  - Improve title matching and normalization
  - Enrich job metadata
  - Perform fuzzy or semantic matching
- Maintain strict separation between job discovery and job application

### Non-Goals
- Requiring LLM credentials for basic functionality
- Auto-apply to jobs
- Handling authentication-only portals in crawler mode
- Hiding core functionality behind proprietary APIs


### Goals
- Accept a list of companies and remember each company’s career site URL
- Crawl each career site automatically (no manual browser)
- Search for multiple job titles per company
- Extract job title, company, location, short description, and job link
- Persist results to disk in a deterministic, machine-readable format
- Enable downstream agents to consume results without re-crawling

### Non-Goals
- Auto-apply to jobs
- Handle authentication-only job portals (out of scope for crawler mode)
- Human-like browsing or decision-making

---

## 3. High-Level Architecture

```
+-------------------+
| User Input        |
| - company list    |
| - title list      |
+---------+---------+
          |
          v
+-------------------+
| Company Registry  |  (persistent)
| - name            |
| - career_url      |
| - ats_type        |
+---------+---------+
          |
          v
+-------------------+
| Crawl Orchestrator|
+---------+---------+
          |
          v
+-------------------+
| Career Crawler    |
| - ATS adapters    |
| - generic fallback|
+---------+---------+
          |
          v
+-------------------+
| Job Filter        |
| - title matching  |
+---------+---------+
          |
          v
+-------------------+
| Job Store         |
| - file system     |
| - dedup           |
+-------------------+
```

---

## 4. Inputs

### 4.1 Company List
Provided as a YAML or JSON file:

```yaml
companies:
  - name: OpenAI
    career_url: https://openai.com/careers
  - name: Anthropic
    career_url: https://www.anthropic.com/careers
```

This input format is identical in both **LLM and non-LLM modes**.

If `career_url` is missing, behavior depends on execution mode:
- **Non-LLM mode**: require explicit `career_url`
- **LLM mode**: optionally discover and validate career URLs automatically

1. Search for "<company> careers"
2. Resolve and validate the career page
3. Persist it for future runs

---

### 4.2 Job Title List

```yaml
titles:
  - Machine Learning Engineer
  - Applied Scientist
  - Research Scientist
```

In **non-LLM mode**:
- Exact and token-based matching only

In **LLM mode**:
- Optional semantic normalization and fuzzy matching

Matching is case-insensitive and supports partial matches and normalization (e.g. "ML Engineer" ≈ "Machine Learning Engineer").

---

## 5. Company Registry (Persistent Memory)

The system maintains a **Company Registry** (e.g. `companies_registry.json`).

```json
{
  "OpenAI": {
    "career_url": "https://openai.com/careers",
    "ats_type": "greenhouse",
    "last_crawled": "2026-01-20"
  }
}
```

Responsibilities:
- Remember career URLs across runs
- Record detected ATS platform
- Enable faster future crawls

---

## 6. Career Crawling Layer

### 6.1 ATS Detection
At crawl time:
- Inspect URLs, HTML patterns, JS variables
- Classify ATS type:
  - Greenhouse
  - Lever
  - Ashby
  - Workday
  - Custom / Unknown

### 6.2 ATS Adapters
Each ATS adapter implements:

```python
interface CareerFetcher:
    def fetch_job_list(self) -> List[Job]
```

Adapters:
- Handle pagination
- Normalize job fields
- Extract canonical job URLs

### 6.3 Generic Fallback Crawler
Used when ATS is unknown:
- Playwright headless browser
- Extract visible links containing job-like keywords
- Conservative crawling depth (≤ 2)

---

## 7. Job Filtering (Title-Based)

Job filtering behavior depends on execution mode.

### 7.1 Non-LLM Mode (Default)

- Case-insensitive string matching
- Token overlap scoring
- Simple synonym dictionary (static, configurable)

```python
match = token_overlap(job.title, desired_titles)
```

This mode guarantees:
- No external API calls
- Fully offline, reproducible behavior

---

### 7.2 LLM-Enhanced Mode (Optional)

When enabled:
- LLM-based fuzzy title matching
- Optional job title normalization
- Optional role seniority inference

```python
match = llm_match(job.title, desired_titles)
```

LLM usage is:
- Explicitly opt-in
- Guarded by configuration flags
- Never required for baseline functionality


After fetching all jobs for a company:

```python
for job in jobs:
    if match(job.title, desired_titles):
        keep(job)
```

Matching strategies:
- Token overlap
- Synonym expansion (optional)
- Optional LLM-based fuzzy matching (configurable)

---

## 8. Job Storage (File-Based)

### 8.1 Folder Structure

```
job_results/
  YYYY-MM-DD/
    OpenAI/
      jobs.json
      jobs.csv
    Anthropic/
      jobs.json
```

### 8.2 Job Schema

```json
{
  "company": "OpenAI",
  "title": "Applied Scientist",
  "location": "San Francisco, CA",
  "team": "Research",
  "url": "https://...",
  "source": "greenhouse",
  "discovered_at": "2026-01-22"
}
```

### 8.3 Deduplication
- Dedup key: `(company, canonical_job_url)`
- Skip previously seen jobs unless content hash changes

---

## 9. Orchestration & Scheduling

### 9.1 One-Off Run

```bash
python search.py \
  --companies companies.yaml \
  --titles titles.yaml \
  --output job_results/
```

### 9.2 Scheduled Run
- Daily cron job
- Only new jobs written
- Optional summary report

---

## 10. Interface with Job Application Agent

The Job Application Agent:
- Reads from `job_results/`
- Selects jobs based on its own criteria
- Never triggers crawling

This interface is **identical in both LLM and non-LLM modes**, ensuring:
- Shareability
- Deterministic downstream behavior

---

## 11. Execution Modes

### 11.1 Non-LLM Mode (Default)

- Enabled by default
- No LLM credentials required
- Supported features:
  - Company registry
  - Career site crawling
  - ATS adapters
  - Title-based filtering
  - File-based job storage

```bash
python search.py --mode no-llm
```

---

### 11.2 LLM-Enhanced Mode (Optional)

- Explicitly enabled by user
- Requires LLM API key
- Adds optional intelligence layers:
  - Semantic title matching
  - Metadata enrichment
  - Job clustering or scoring

```bash
python search.py --mode llm --llm-provider anthropic
```

Failure to configure LLM credentials **must never break** the non-LLM pipeline.

---

## 12. Error Handling & Safety

- Crawl timeout per company
- Max pages per site
- Hard stop on login / apply pages
- Domain change detection
- LLM failure isolation (LLM errors do not affect non-LLM pipeline)

---

## 13. Shareability & Distribution

- Default installation runs fully without LLMs
- LLM dependencies are optional extras
- Clear documentation of capability differences
- Safe to share with users who lack API keys

---

## 14. Future Extensions (Optional)

- Chrome takeover fallback (manual enable)
- Job change detection (JD diffing)
- Embedding-based title matching
- RAG over historical job postings

---

## 15. Summary

This design ensures:
- A fully functional, shareable crawler without LLM dependencies
- Optional intelligence layers for advanced users
- Clean separation of concerns and failure domains
- Deterministic, scalable job discovery
- Persistent company-level memory
- Clean handoff to downstream agents
- Minimal coupling and high extensibility

**Non-LLM first. LLM optional. Shareable by default.**  
**Crawler-first, agent-second, memory-driven.**

