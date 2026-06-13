# ZenodoPipeline

Automated Zenodo DOI publisher for research repositories. Scans project directories, extracts metadata from README/LICENSE/E156-PROTOCOL/git, builds zip archives, and publishes to Zenodo for DOI minting.

## Features

- **Metadata extraction** from README.md (title, description), LICENSE (SPDX detection), E156-PROTOCOL.md (body, dates), and git log (authors)
- **Archive builder** with smart include/exclude rules (no secrets, no caches, no config dirs)
- **Zenodo API client** for sandbox and production (create deposition, upload, publish)
- **Batch mode** processes multiple projects from INDEX.md filtered by status
- **Dry-run mode** previews everything without API calls or tokens
- **Post-publish updates** add DOI badges to README.md and E156-PROTOCOL.md

## Safety

- **Sandbox by default** -- production requires explicit `--publish` flag
- **Dry-run needs no token** -- works completely offline
- **Tokens via environment variables only** -- never hardcoded, never printed
- **Excluded from archives**: `.git/`, `__pycache__/`, `.env`, `.claude/`, `.gemini/`, `.codex/`, `PROGRESS.md`, `chromedriver*`
- **50 MB per-file limit** enforced (Zenodo constraint)

## Usage

```bash
# Dry-run: see what would be uploaded (no token needed)
python zenodo_publish.py --project /path/to/project --dry-run

# Upload to sandbox (requires ZENODO_SANDBOX_TOKEN)
python zenodo_publish.py --project /path/to/project --sandbox

# Publish to production and mint DOI (requires ZENODO_TOKEN)
python zenodo_publish.py --project /path/to/project --publish

# Batch: process all SUBMISSION-READY projects
python zenodo_publish.py --batch --index /path/to/INDEX.md --dry-run
python zenodo_publish.py --batch --index /path/to/INDEX.md --sandbox
python zenodo_publish.py --batch --index /path/to/INDEX.md --status SUBMISSION-READY --publish

# Verbose output
python zenodo_publish.py --project /path/to/project --dry-run --verbose
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ZENODO_SANDBOX_TOKEN` | API token for sandbox.zenodo.org |
| `ZENODO_TOKEN` | API token for production zenodo.org |

Get tokens at: https://zenodo.org/account/settings/applications/ (or sandbox equivalent)

## CLI Arguments

| Argument | Description |
|----------|-------------|
| `--project PATH` | Single project directory |
| `--batch` | Process multiple projects from INDEX.md |
| `--index PATH` | Path to INDEX.md (required with `--batch`) |
| `--status STATUS` | Filter by status in batch mode (default: `SUBMISSION-READY`) |
| `--sandbox` | Use sandbox.zenodo.org (default) |
| `--publish` | Use production zenodo.org and publish for real DOI |
| `--dry-run` | Preview without API calls |
| `--output-dir PATH` | Directory for archive zips (default: temp) |
| `--verbose` | Verbose output |

## Testing

```bash
python -m pytest test_zenodo.py -v
```

All 36 tests use mocked API calls -- no real network requests.

## Requirements

- Python 3.10+
- `requests` (only needed for actual uploads, not dry-run)
- `pytest` (for testing)

## Author

Mahmood Ahmad, Tahir Heart Institute
