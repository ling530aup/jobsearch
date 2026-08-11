CREATE DATABASE IF NOT EXISTS jobsearch
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE jobsearch;

CREATE TABLE IF NOT EXISTS companies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    career_url VARCHAR(1024) NOT NULL,
    ats_type VARCHAR(64) NULL,
    last_crawled DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_companies_name (name),
    KEY idx_companies_last_crawled (last_crawled)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    status VARCHAR(32) NOT NULL,
    companies_total INT UNSIGNED NOT NULL DEFAULT 0,
    companies_succeeded INT UNSIGNED NOT NULL DEFAULT 0,
    companies_failed INT UNSIGNED NOT NULL DEFAULT 0,
    jobs_fetched INT UNSIGNED NOT NULL DEFAULT 0,
    jobs_saved INT UNSIGNED NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    PRIMARY KEY (id),
    KEY idx_crawl_runs_started_at (started_at),
    KEY idx_crawl_runs_status (status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    company_id BIGINT UNSIGNED NOT NULL,
    crawl_run_id BIGINT UNSIGNED NOT NULL,
    canonical_url_hash CHAR(64) CHARACTER SET ascii NOT NULL,
    url VARCHAR(2048) NOT NULL,
    title VARCHAR(512) NOT NULL,
    location VARCHAR(512) NULL,
    team VARCHAR(255) NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'unknown',
    discovered_at DATETIME(6) NOT NULL,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_jobs_company_url (company_id, canonical_url_hash),
    KEY idx_jobs_company_discovered (company_id, discovered_at),
    KEY idx_jobs_run_discovered (crawl_run_id, discovered_at),
    KEY idx_jobs_applied_discovered (applied, discovered_at),
    KEY idx_jobs_location (location(100)),
    CONSTRAINT fk_jobs_company FOREIGN KEY (company_id) REFERENCES companies(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_jobs_crawl_run FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;
