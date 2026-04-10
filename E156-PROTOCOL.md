# E156 Protocol

Project: ZenodoPipeline
Created: 2026-04-09
Dashboard: N/A (CLI tool)

## Body

Can automated metadata extraction from README, LICENSE, git log, and E156-PROTOCOL files produce valid Zenodo depositions for batch DOI minting across 276+ research repositories? We built ZenodoPipeline, a Python CLI tool that scans project directories, detects license type via regex pattern matching (MIT, Apache-2.0, GPL-3.0, and 6 others), extracts authors ordered by commit frequency, and builds filtered zip archives excluding secrets and caches. The tool processes single projects or batches from INDEX.md filtered by status (e.g., SUBMISSION-READY), with a sandbox-first safety model requiring explicit --publish for production. In dry-run testing across representative project directories, metadata extraction correctly identified titles, descriptions, licenses, and file inventories with zero false inclusions of .env, .claude/, or PROGRESS.md files. All 36 unit tests pass with mocked Zenodo API calls covering deposition creation, file upload, metadata update, and DOI retrieval. ZenodoPipeline enables systematic DOI assignment for open-access research tools without manual Zenodo form-filling. The tool assumes git-based projects with README.md and does not handle versioned DOI updates or community-specific metadata beyond the default Zenodo schema.
