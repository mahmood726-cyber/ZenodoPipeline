#!/usr/bin/env python
"""
ZenodoPipeline: Automated Zenodo DOI publisher for research repositories.

Usage:
    python zenodo_publish.py --project C:\\Models\\Asa\\ --sandbox --dry-run
    python zenodo_publish.py --project C:\\Models\\Asa\\ --sandbox
    python zenodo_publish.py --project C:\\Models\\Asa\\ --publish
    python zenodo_publish.py --batch --index C:\\ProjectIndex\\INDEX.md --sandbox --dry-run
    python zenodo_publish.py --batch --index C:\\ProjectIndex\\INDEX.md --sandbox --status SUBMISSION-READY

Environment:
    ZENODO_TOKEN: API token for production (required for actual upload)
    ZENODO_SANDBOX_TOKEN: Sandbox API token (for --sandbox mode)

Safety:
    - Sandbox by default; production requires explicit --publish
    - Dry-run works WITHOUT any token (no API calls)
    - Tokens are NEVER printed or logged
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INCLUDE_EXTENSIONS = {
    ".py", ".html", ".md", ".json", ".csv", ".txt", ".r", ".js",
    ".css", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".bat", ".sh",
    ".rmd", ".qmd", ".ipynb",
}

# Filenames without extension that should always be included
INCLUDE_NAMES = {"LICENSE", "Makefile", "Dockerfile", "CITATION.cff"}

EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".claude", ".gemini", ".codex",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox", "dist",
    "build", "egg-info",
}

EXCLUDE_FILES = {
    ".env", ".env.local", ".env.production", "PROGRESS.md",
    "chromedriver", "chromedriver.exe",
}

EXCLUDE_EXTENSIONS = {".pyc", ".pyo", ".log", ".bak", ".swp", ".swo"}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Zenodo per-file limit

LICENSE_PATTERNS = {
    "MIT": [r"MIT License", r"Permission is hereby granted, free of charge"],
    "Apache-2.0": [r"Apache License.*Version 2\.0", r"apache\.org/licenses/LICENSE-2\.0"],
    "GPL-3.0": [r"GNU GENERAL PUBLIC LICENSE.*Version 3", r"gnu\.org/licenses/gpl-3\.0"],
    "GPL-2.0": [r"GNU GENERAL PUBLIC LICENSE.*Version 2"],
    "BSD-3-Clause": [r"BSD 3-Clause", r"Redistribution and use.*three conditions"],
    "BSD-2-Clause": [r"BSD 2-Clause", r"Redistribution and use.*two conditions"],
    "ISC": [r"ISC License"],
    "CC-BY-4.0": [r"Creative Commons Attribution 4\.0"],
    "Unlicense": [r"This is free and unencumbered software"],
}

DEFAULT_AUTHOR = {
    "name": "Ahmad, Mahmood",
    "affiliation": "Tahir Heart Institute",
    "orcid": "0009-0003-7781-4478",
}


# ---------------------------------------------------------------------------
# Metadata Extraction
# ---------------------------------------------------------------------------

def detect_license(project_dir):
    """Detect license type from LICENSE file content."""
    license_path = Path(project_dir) / "LICENSE"
    if not license_path.exists():
        # Try common variants
        for name in ("LICENSE.md", "LICENSE.txt", "LICENCE", "COPYING"):
            alt = Path(project_dir) / name
            if alt.exists():
                license_path = alt
                break
        else:
            return "other-open"

    try:
        content = license_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "other-open"

    for spdx_id, patterns in LICENSE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, content, re.IGNORECASE):
                return spdx_id
    return "other-open"


def extract_readme_metadata(project_dir):
    """Extract title and description from README.md."""
    readme = Path(project_dir) / "README.md"
    title = Path(project_dir).name
    description = ""

    if not readme.exists():
        return title, description

    try:
        lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return title, description

    # Title = first # heading
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped.lstrip("# ").strip()
            break

    # Description = first non-empty, non-heading paragraph after title
    in_description = False
    desc_lines = []
    found_title = False
    for line in lines:
        stripped = line.strip()
        if not found_title:
            if stripped.startswith("# "):
                found_title = True
            continue
        # Skip badges, blank lines, sub-headings before first paragraph
        if not in_description:
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("[!["):
                continue
            in_description = True
        if in_description:
            if not stripped:
                break  # end of paragraph
            desc_lines.append(stripped)

    description = " ".join(desc_lines)
    return title, description


def extract_e156_metadata(project_dir):
    """Extract body and dates from E156-PROTOCOL.md if present."""
    proto = Path(project_dir) / "E156-PROTOCOL.md"
    body = ""
    dates = ""

    if not proto.exists():
        return body, dates

    try:
        content = proto.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return body, dates

    # Look for Body section
    body_match = re.search(
        r"(?:^|\n)#+\s*(?:Body|Abstract|E156 Body)[^\n]*\n+(.*?)(?=\n#+|\Z)",
        content, re.IGNORECASE | re.DOTALL,
    )
    if body_match:
        body = body_match.group(1).strip()

    # Look for dates
    date_match = re.search(r"(?:Date|Created|Started)\s*[:=]\s*(\d{4}-\d{2}-\d{2})", content, re.IGNORECASE)
    if date_match:
        dates = date_match.group(1)

    return body, dates


def extract_git_authors(project_dir):
    """Extract unique authors from git log, ordered by commit count."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%aN <%aE>", "--no-merges"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return []
    except (subprocess.SubprocessError, FileNotFoundError):
        return []

    counts = {}
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            counts[line] = counts.get(line, 0) + 1

    # Sort by commit count descending
    sorted_authors = sorted(counts.items(), key=lambda x: -x[1])
    return [author for author, _ in sorted_authors]


def count_project_stats(project_dir):
    """Count lines of code, test files, and languages used."""
    loc = 0
    test_files = 0
    languages = set()
    ext_to_lang = {
        ".py": "Python", ".js": "JavaScript", ".html": "HTML",
        ".css": "CSS", ".r": "R", ".rmd": "R", ".ts": "TypeScript",
        ".java": "Java", ".cpp": "C++", ".c": "C", ".sh": "Shell",
    }

    project = Path(project_dir)
    for fpath in project.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in fpath.parts):
            continue
        if not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext in ext_to_lang:
            languages.add(ext_to_lang[ext])
        if ext in INCLUDE_EXTENSIONS:
            try:
                loc += sum(1 for _ in fpath.open(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        fname_lower = fpath.name.lower()
        if fname_lower.startswith("test_") or fname_lower.endswith("_test.py"):
            test_files += 1

    return {"lines_of_code": loc, "test_files": test_files, "languages": sorted(languages)}


def extract_metadata(project_dir):
    """
    Scan project directory for metadata.

    Returns dict with: title, description, authors, license, keywords,
    e156_body, e156_date, stats, project_dir.
    """
    project_dir = Path(project_dir).resolve()
    title, description = extract_readme_metadata(project_dir)
    e156_body, e156_date = extract_e156_metadata(project_dir)
    git_authors = extract_git_authors(project_dir)
    license_id = detect_license(project_dir)
    stats = count_project_stats(project_dir)

    # Build keywords from languages + generic
    keywords = ["meta-analysis", "evidence-based-medicine", "open-access"]
    for lang in stats["languages"]:
        keywords.append(lang.lower())

    # Parse authors into structured format
    authors = []
    for raw in git_authors:
        name_match = re.match(r"^(.+?)\s*<", raw)
        if name_match:
            full_name = name_match.group(1).strip()
            parts = full_name.split()
            if len(parts) >= 2:
                formatted = f"{parts[-1]}, {' '.join(parts[:-1])}"
            else:
                formatted = full_name
            authors.append({"name": formatted})

    # Ensure default author is present
    if not authors:
        authors = [DEFAULT_AUTHOR.copy()]
    else:
        # Check if default author already present
        default_surname = DEFAULT_AUTHOR["name"].split(",")[0].lower()
        found = any(default_surname in a["name"].lower() for a in authors)
        if found:
            # Enrich with affiliation/orcid
            for a in authors:
                if default_surname in a["name"].lower():
                    a["affiliation"] = DEFAULT_AUTHOR["affiliation"]
                    a["orcid"] = DEFAULT_AUTHOR["orcid"]

    return {
        "title": title,
        "description": description or f"{title}: an open-access research tool.",
        "authors": authors,
        "license": license_id,
        "keywords": keywords,
        "e156_body": e156_body,
        "e156_date": e156_date,
        "stats": stats,
        "project_dir": str(project_dir),
    }


# ---------------------------------------------------------------------------
# .zenodo.json Generation
# ---------------------------------------------------------------------------

def generate_zenodo_json(metadata):
    """
    Generate .zenodo.json dict conforming to Zenodo metadata schema.
    """
    creators = []
    for a in metadata["authors"]:
        entry = {"name": a["name"]}
        if "affiliation" in a:
            entry["affiliation"] = a["affiliation"]
        if "orcid" in a:
            entry["orcid"] = a["orcid"]
        creators.append(entry)

    project_name = Path(metadata["project_dir"]).name

    zenodo_meta = {
        "title": metadata["title"],
        "upload_type": "software",
        "description": metadata["description"],
        "creators": creators,
        "license": metadata["license"],
        "keywords": metadata["keywords"],
        "communities": [{"identifier": "zenodo"}],
        "access_right": "open",
        "related_identifiers": [
            {
                "identifier": f"https://github.com/mahmood726-cyber/{project_name}",
                "relation": "isSupplementTo",
                "scheme": "url",
            }
        ],
    }
    return zenodo_meta


# ---------------------------------------------------------------------------
# Archive Builder
# ---------------------------------------------------------------------------

def should_include_file(fpath, project_dir):
    """Determine if a file should be included in the archive."""
    rel = fpath.relative_to(project_dir)
    parts = rel.parts

    # Exclude entire directories
    for part in parts:
        if part in EXCLUDE_DIRS:
            return False
        # Catch .egg-info dirs
        if part.endswith(".egg-info"):
            return False

    fname = fpath.name

    # Exclude specific files
    if fname in EXCLUDE_FILES:
        return False

    # Exclude by extension
    ext = fpath.suffix.lower()
    if ext in EXCLUDE_EXTENSIONS:
        return False

    # Exclude chromedriver variants
    if fname.lower().startswith("chromedriver"):
        return False

    # Include by name
    if fname in INCLUDE_NAMES:
        return True

    # Include by extension
    if ext in INCLUDE_EXTENSIONS:
        return True

    return False


def build_archive(project_dir, output_path):
    """
    Create zip archive of project, respecting include/exclude rules.

    Returns dict with: file_count, total_size, skipped_large, included_files, excluded_files.
    """
    project_dir = Path(project_dir).resolve()
    output_path = Path(output_path)

    included_files = []
    excluded_files = []
    skipped_large = []
    total_size = 0

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(project_dir.rglob("*")):
            if not fpath.is_file():
                continue

            if not should_include_file(fpath, project_dir):
                excluded_files.append(str(fpath.relative_to(project_dir)))
                continue

            file_size = fpath.stat().st_size
            if file_size > MAX_FILE_SIZE:
                skipped_large.append(
                    (str(fpath.relative_to(project_dir)), file_size)
                )
                continue

            arcname = str(fpath.relative_to(project_dir))
            zf.write(fpath, arcname)
            included_files.append(arcname)
            total_size += file_size

    return {
        "file_count": len(included_files),
        "total_size": total_size,
        "skipped_large": skipped_large,
        "included_files": included_files,
        "excluded_files": excluded_files,
    }


# ---------------------------------------------------------------------------
# Zenodo API Client
# ---------------------------------------------------------------------------

class ZenodoClient:
    """REST API client for Zenodo (sandbox or production)."""

    def __init__(self, token, sandbox=True):
        self.base_url = (
            "https://sandbox.zenodo.org" if sandbox else "https://zenodo.org"
        )
        self.token = token
        # Import requests lazily so dry-run works without it
        import requests as _requests
        self._requests = _requests

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def create_deposition(self):
        """Create empty deposition. Returns (deposition_id, bucket_url)."""
        resp = self._requests.post(
            f"{self.base_url}/api/deposit/depositions",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data["links"]["bucket"]

    def upload_file(self, bucket_url, filepath):
        """Upload a file to the deposition bucket."""
        filepath = Path(filepath)
        with open(filepath, "rb") as fp:
            resp = self._requests.put(
                f"{bucket_url}/{filepath.name}",
                data=fp,
                headers=self._headers(),
            )
        resp.raise_for_status()
        return resp.json()

    def update_metadata(self, deposition_id, metadata):
        """Set metadata on deposition."""
        resp = self._requests.put(
            f"{self.base_url}/api/deposit/depositions/{deposition_id}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"metadata": metadata},
        )
        resp.raise_for_status()
        return resp.json()

    def publish(self, deposition_id):
        """Publish deposition. Returns DOI string."""
        resp = self._requests.post(
            f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/publish",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("doi", data.get("metadata", {}).get("doi", ""))

    def get_doi(self, deposition_id):
        """Get DOI from an existing deposition."""
        resp = self._requests.get(
            f"{self.base_url}/api/deposit/depositions/{deposition_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("doi", data.get("metadata", {}).get("doi", ""))

    def list_depositions(self, status=None):
        """List depositions, optionally filtered by status."""
        params = {}
        if status:
            params["status"] = status
        resp = self._requests.get(
            f"{self.base_url}/api/deposit/depositions",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Post-Publish Updates
# ---------------------------------------------------------------------------

def update_readme_with_doi(readme_path, doi):
    """Add DOI badge to the top of README.md (after first heading)."""
    readme_path = Path(readme_path)
    if not readme_path.exists():
        return False

    content = readme_path.read_text(encoding="utf-8", errors="replace")
    badge = f"[![DOI](https://zenodo.org/badge/DOI/{doi}.svg)](https://doi.org/{doi})"

    # Don't duplicate
    if doi in content:
        return False

    lines = content.splitlines(keepends=True)
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("# "):
            insert_idx = i + 1
            break

    lines.insert(insert_idx, "\n" + badge + "\n")
    readme_path.write_text("".join(lines), encoding="utf-8")
    return True


def update_protocol_with_doi(protocol_path, doi):
    """Add DOI line to E156-PROTOCOL.md."""
    proto = Path(protocol_path)
    if not proto.exists():
        return False

    content = proto.read_text(encoding="utf-8", errors="replace")
    if doi in content:
        return False

    doi_line = f"\n**DOI**: [{doi}](https://doi.org/{doi})\n"
    content += doi_line
    proto.write_text(content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Batch Mode
# ---------------------------------------------------------------------------

def parse_index_md(index_path, status_filter="SUBMISSION-READY"):
    """
    Parse INDEX.md table rows to find projects matching status_filter.

    Returns list of dicts with keys: name, location, status.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return []

    content = index_path.read_text(encoding="utf-8", errors="replace")
    projects = []

    # Match table rows: | # | **Name** | `Location` | ... | Status | ...
    # The table has columns: #, Project, Location, Type, Lines/Tests, Paper Status, Target Journal, Last Touch
    row_pattern = re.compile(
        r"\|\s*\d+\w?\s*\|\s*\*\*(.+?)\*\*\s*\|\s*`(.+?)`\s*\|"
        r"[^|]*\|[^|]*\|\s*(.+?)\s*\|",
    )

    for match in row_pattern.finditer(content):
        name = match.group(1).strip()
        location = match.group(2).strip()
        status = match.group(3).strip()

        if status_filter.upper() in status.upper():
            projects.append({
                "name": name,
                "location": location,
                "status": status,
            })

    return projects


def batch_process(index_path, status_filter="SUBMISSION-READY", sandbox=True,
                  dry_run=False, output_dir=None, verbose=False):
    """
    Parse INDEX.md, find projects matching status, process each.

    Returns summary dict with: processed, skipped, failed, dois.
    """
    projects = parse_index_md(index_path, status_filter)
    summary = {"processed": 0, "skipped": 0, "failed": 0, "dois": []}

    if not projects:
        print(f"No projects found with status matching '{status_filter}'.")
        return summary

    print(f"Found {len(projects)} project(s) matching '{status_filter}':")
    for p in projects:
        print(f"  - {p['name']} ({p['location']})")
    print()

    for proj in projects:
        loc = proj["location"]
        if not Path(loc).exists():
            print(f"  SKIP {proj['name']}: directory not found at {loc}")
            summary["skipped"] += 1
            continue

        try:
            if dry_run:
                dry_run_report(loc, verbose=verbose)
                summary["processed"] += 1
            else:
                doi = process_single_project(
                    loc, sandbox=sandbox, output_dir=output_dir, verbose=verbose,
                )
                summary["processed"] += 1
                if doi:
                    summary["dois"].append({"project": proj["name"], "doi": doi})
        except Exception as exc:
            print(f"  FAIL {proj['name']}: {exc}")
            summary["failed"] += 1

    return summary


# ---------------------------------------------------------------------------
# Single Project Processing
# ---------------------------------------------------------------------------

def process_single_project(project_dir, sandbox=True, output_dir=None,
                           verbose=False, do_publish=False):
    """
    Full pipeline for a single project: extract, archive, upload.

    Returns DOI string if published, else None.
    """
    project_dir = Path(project_dir).resolve()
    print(f"Processing: {project_dir.name}")

    # 1. Extract metadata
    metadata = extract_metadata(project_dir)
    zenodo_meta = generate_zenodo_json(metadata)

    if verbose:
        print(f"  Title: {metadata['title']}")
        print(f"  License: {metadata['license']}")
        print(f"  Authors: {len(metadata['authors'])}")

    # 2. Write .zenodo.json
    zenodo_json_path = project_dir / ".zenodo.json"
    zenodo_json_path.write_text(
        json.dumps(zenodo_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Wrote .zenodo.json")

    # 3. Build archive
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(tempfile.mkdtemp(prefix="zenodo_"))

    archive_path = out / f"{project_dir.name}.zip"
    archive_info = build_archive(project_dir, archive_path)
    print(f"  Archive: {archive_info['file_count']} files, "
          f"{archive_info['total_size'] / 1024:.1f} KB")
    if archive_info["skipped_large"]:
        for fname, fsize in archive_info["skipped_large"]:
            print(f"  WARNING: Skipped {fname} ({fsize / 1024 / 1024:.1f} MB > 50 MB)")

    # 4. Get token
    token_var = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_TOKEN"
    token = os.environ.get(token_var)
    if not token:
        print(f"  ERROR: {token_var} not set. Cannot upload.")
        return None

    # 5. Upload
    mode_label = "sandbox" if sandbox else "PRODUCTION"
    print(f"  Uploading to Zenodo ({mode_label})...")
    client = ZenodoClient(token, sandbox=sandbox)

    dep_id, bucket_url = client.create_deposition()
    if verbose:
        print(f"  Deposition ID: {dep_id}")

    client.upload_file(bucket_url, archive_path)
    client.update_metadata(dep_id, zenodo_meta)
    print(f"  Uploaded. Deposition ID: {dep_id}")

    # 6. Publish if requested
    doi = None
    if do_publish:
        doi = client.publish(dep_id)
        print(f"  Published! DOI: {doi}")

        # Update project files
        readme_path = project_dir / "README.md"
        if readme_path.exists():
            update_readme_with_doi(readme_path, doi)
        proto_path = project_dir / "E156-PROTOCOL.md"
        if proto_path.exists():
            update_protocol_with_doi(proto_path, doi)
    else:
        print(f"  Deposition created (not published). Use --publish to mint DOI.")

    return doi


# ---------------------------------------------------------------------------
# Dry-Run
# ---------------------------------------------------------------------------

def dry_run_report(project_dir, verbose=False):
    """
    Print what would be uploaded without making any API calls.
    No token required.
    """
    project_dir = Path(project_dir).resolve()
    print(f"\n{'='*60}")
    print(f"DRY RUN: {project_dir.name}")
    print(f"{'='*60}")

    metadata = extract_metadata(project_dir)
    zenodo_meta = generate_zenodo_json(metadata)

    print(f"  Title:       {metadata['title']}")
    print(f"  Description: {metadata['description'][:100]}...")
    print(f"  License:     {metadata['license']}")
    print(f"  Authors:     {', '.join(a['name'] for a in metadata['authors'])}")
    print(f"  Keywords:    {', '.join(metadata['keywords'])}")
    print(f"  Languages:   {', '.join(metadata['stats']['languages']) or 'N/A'}")
    print(f"  LOC:         {metadata['stats']['lines_of_code']}")
    print(f"  Test files:  {metadata['stats']['test_files']}")

    if metadata["e156_body"]:
        print(f"  E156 body:   {metadata['e156_body'][:80]}...")
    if metadata["e156_date"]:
        print(f"  E156 date:   {metadata['e156_date']}")

    # Simulate archive
    included = []
    excluded = []
    oversized = []
    total_size = 0

    for fpath in sorted(project_dir.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(project_dir))
        if should_include_file(fpath, project_dir):
            fsize = fpath.stat().st_size
            if fsize > MAX_FILE_SIZE:
                oversized.append((rel, fsize))
            else:
                included.append((rel, fsize))
                total_size += fsize
        else:
            excluded.append(rel)

    print(f"\n  Files to include: {len(included)} ({total_size / 1024:.1f} KB)")
    if verbose:
        for name, sz in included:
            print(f"    + {name} ({sz / 1024:.1f} KB)")

    print(f"  Files excluded:   {len(excluded)}")
    if verbose:
        for name in excluded[:20]:
            print(f"    - {name}")
        if len(excluded) > 20:
            print(f"    ... and {len(excluded) - 20} more")

    if oversized:
        print(f"  Files too large (>50MB): {len(oversized)}")
        for name, sz in oversized:
            print(f"    ! {name} ({sz / 1024 / 1024:.1f} MB)")

    print(f"\n  .zenodo.json preview:")
    print(f"    {json.dumps(zenodo_meta, indent=4)[:500]}")

    sandbox_token = os.environ.get("ZENODO_SANDBOX_TOKEN")
    prod_token = os.environ.get("ZENODO_TOKEN")
    print(f"\n  Sandbox token: {'SET' if sandbox_token else 'NOT SET'}")
    print(f"  Production token: {'SET' if prod_token else 'NOT SET'}")
    print(f"  Target: sandbox.zenodo.org (default)")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="ZenodoPipeline: Automated Zenodo DOI publisher for research repositories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", type=str, help="Single project directory")
    group.add_argument("--batch", action="store_true", help="Batch mode: process from INDEX.md")

    parser.add_argument("--index", type=str, default=r"C:\ProjectIndex\INDEX.md",
                        help="Path to INDEX.md (with --batch)")
    parser.add_argument("--status", type=str, default="SUBMISSION-READY",
                        help="Filter by status (with --batch, default: SUBMISSION-READY)")
    parser.add_argument("--sandbox", action="store_true", default=True,
                        help="Use sandbox.zenodo.org (default)")
    parser.add_argument("--publish", action="store_true",
                        help="Use production zenodo.org and publish for real DOI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without API calls")
    parser.add_argument("--output-dir", type=str,
                        help="Directory for archive zips (default: temp)")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose output")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    sandbox = not args.publish

    if args.batch:
        summary = batch_process(
            index_path=args.index,
            status_filter=args.status,
            sandbox=sandbox,
            dry_run=args.dry_run,
            output_dir=args.output_dir,
            verbose=args.verbose,
        )
        print("\n--- Batch Summary ---")
        print(f"  Processed: {summary['processed']}")
        print(f"  Skipped:   {summary['skipped']}")
        print(f"  Failed:    {summary['failed']}")
        if summary["dois"]:
            print("  DOIs minted:")
            for d in summary["dois"]:
                print(f"    {d['project']}: {d['doi']}")
    elif args.project:
        project_dir = args.project
        if not Path(project_dir).exists():
            print(f"ERROR: Project directory not found: {project_dir}")
            sys.exit(1)

        if args.dry_run:
            dry_run_report(project_dir, verbose=args.verbose)
        else:
            doi = process_single_project(
                project_dir,
                sandbox=sandbox,
                output_dir=args.output_dir,
                verbose=args.verbose,
                do_publish=args.publish,
            )
            if doi:
                print(f"\nDOI: {doi}")


if __name__ == "__main__":
    main()
