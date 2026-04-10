"""
Test suite for ZenodoPipeline.

All tests mock the Zenodo API — NO real uploads.
Uses temporary directories with minimal test fixtures.
"""

import json
import os
import sys
import tempfile
import textwrap
import zipfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the module is importable
sys.path.insert(0, str(Path(__file__).parent))

from zenodo_publish import (
    DEFAULT_AUTHOR,
    EXCLUDE_DIRS,
    EXCLUDE_FILES,
    ZenodoClient,
    batch_process,
    build_archive,
    detect_license,
    dry_run_report,
    extract_metadata,
    extract_readme_metadata,
    extract_e156_metadata,
    extract_git_authors,
    generate_zenodo_json,
    parse_index_md,
    should_include_file,
    update_readme_with_doi,
    update_protocol_with_doi,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal temporary project directory."""
    # README.md
    (tmp_path / "README.md").write_text(
        "# Test Project Alpha\n\n"
        "A tool for testing Zenodo integration.\n"
        "It does many things.\n\n"
        "## Installation\n\nRun pip install.\n",
        encoding="utf-8",
    )

    # LICENSE (MIT)
    (tmp_path / "LICENSE").write_text(
        "MIT License\n\n"
        "Copyright (c) 2026 Mahmood Ahmad\n\n"
        "Permission is hereby granted, free of charge, to any person...\n",
        encoding="utf-8",
    )

    # E156-PROTOCOL.md
    (tmp_path / "E156-PROTOCOL.md").write_text(
        "# E156 Protocol\n\n"
        "Project: Test Project Alpha\n"
        "Date: 2026-04-01\n\n"
        "## Body\n\n"
        "Does SGLT2i reduce hospitalization in HFpEF patients aged over 65?\n",
        encoding="utf-8",
    )

    # Python files
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_main.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )

    # HTML file
    (tmp_path / "dashboard.html").write_text("<html><body>Hi</body></html>\n", encoding="utf-8")

    # JSON config
    (tmp_path / "config.json").write_text('{"version": 1}\n', encoding="utf-8")

    # Files that should be excluded
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "main.cpython-311.pyc").write_bytes(b"\x00" * 100)

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")

    (tmp_path / ".env").write_text("SECRET=abc\n", encoding="utf-8")

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{}\n", encoding="utf-8")

    (tmp_path / "PROGRESS.md").write_text("# Progress\n", encoding="utf-8")

    return tmp_path


@pytest.fixture
def tmp_index(tmp_path):
    """Create a minimal INDEX.md for batch testing."""
    index_content = textwrap.dedent("""\
        # Project Index

        ## Tier 1

        | # | Project | Location | Type | Lines/Tests | Paper Status | Target Journal | Last Touch |
        |---|---------|----------|------|-------------|-------------|----------------|------------|
        | 1 | **ProjectA** | `{dir_a}` | HTML | 500 / 10 | SUBMISSION-READY | BMJ | 2026-04-01 |
        | 2 | **ProjectB** | `{dir_b}` | Python | 300 / 5 | ACTIVE | Lancet | 2026-04-01 |
        | 3 | **ProjectC** | `{dir_c}` | HTML | 200 / 3 | SUBMISSION-READY | Nature | 2026-04-01 |
        | 4 | **ProjectD** | `C:\\NonExistent\\Path` | Python | 100 / 0 | SUBMISSION-READY | F1000 | 2026-04-01 |
    """)

    dir_a = tmp_path / "project_a"
    dir_b = tmp_path / "project_b"
    dir_c = tmp_path / "project_c"
    dir_a.mkdir()
    dir_b.mkdir()
    dir_c.mkdir()

    for d in (dir_a, dir_b, dir_c):
        (d / "README.md").write_text(f"# {d.name}\n\nDescription.\n", encoding="utf-8")
        (d / "main.py").write_text("x = 1\n", encoding="utf-8")
        (d / "LICENSE").write_text("MIT License\nPermission is hereby granted, free of charge...\n", encoding="utf-8")

    formatted = index_content.format(
        dir_a=str(dir_a),
        dir_b=str(dir_b),
        dir_c=str(dir_c),
    )
    index_file = tmp_path / "INDEX.md"
    index_file.write_text(formatted, encoding="utf-8")

    return index_file, dir_a, dir_b, dir_c


# ---------------------------------------------------------------------------
# 1. extract_metadata: README.md title extraction
# ---------------------------------------------------------------------------

class TestExtractMetadata:

    def test_readme_title_extraction(self, tmp_project):
        """README.md # heading becomes the title."""
        metadata = extract_metadata(tmp_project)
        assert metadata["title"] == "Test Project Alpha"

    def test_readme_description(self, tmp_project):
        """First paragraph after heading becomes description."""
        metadata = extract_metadata(tmp_project)
        assert "testing Zenodo integration" in metadata["description"]

    def test_e156_protocol_parsing(self, tmp_project):
        """E156-PROTOCOL.md body and date are extracted."""
        metadata = extract_metadata(tmp_project)
        assert "SGLT2i" in metadata["e156_body"]
        assert metadata["e156_date"] == "2026-04-01"

    @patch("zenodo_publish.subprocess.run")
    def test_git_author_extraction(self, mock_run, tmp_project):
        """Git log authors are extracted and ordered by commit count."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Mahmood Ahmad <m@example.com>\n"
                "Mahmood Ahmad <m@example.com>\n"
                "Jane Doe <j@example.com>\n"
                "Mahmood Ahmad <m@example.com>\n"
            ),
        )
        authors = extract_git_authors(tmp_project)
        assert len(authors) == 2
        assert "Mahmood Ahmad" in authors[0]
        assert "Jane Doe" in authors[1]

    def test_license_detection_mit(self, tmp_project):
        """MIT license is correctly detected."""
        license_id = detect_license(tmp_project)
        assert license_id == "MIT"

    def test_license_detection_apache(self, tmp_path):
        """Apache-2.0 license is correctly detected."""
        (tmp_path / "LICENSE").write_text(
            "Apache License\nVersion 2.0, January 2004\n"
            "http://www.apache.org/licenses/LICENSE-2.0\n",
            encoding="utf-8",
        )
        assert detect_license(tmp_path) == "Apache-2.0"

    def test_license_detection_gpl3(self, tmp_path):
        """GPL-3.0 license is correctly detected."""
        (tmp_path / "LICENSE").write_text(
            "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
            "https://www.gnu.org/licenses/gpl-3.0\n",
            encoding="utf-8",
        )
        assert detect_license(tmp_path) == "GPL-3.0"

    def test_missing_readme_uses_dirname(self, tmp_path):
        """Without README.md, title falls back to directory name."""
        (tmp_path / "main.py").write_text("x=1\n", encoding="utf-8")
        metadata = extract_metadata(tmp_path)
        assert metadata["title"] == tmp_path.name

    @patch("zenodo_publish.subprocess.run")
    def test_default_author_when_no_git(self, mock_run, tmp_project):
        """When git log fails, default author is used."""
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        metadata = extract_metadata(tmp_project)
        names = [a["name"] for a in metadata["authors"]]
        assert any("Ahmad" in n for n in names)

    def test_stats_counting(self, tmp_project):
        """Line count, test files, and languages are computed."""
        metadata = extract_metadata(tmp_project)
        assert metadata["stats"]["lines_of_code"] > 0
        assert metadata["stats"]["test_files"] >= 1
        assert "Python" in metadata["stats"]["languages"]


# ---------------------------------------------------------------------------
# 7-8. generate_zenodo_json
# ---------------------------------------------------------------------------

class TestGenerateZenodoJson:

    def test_valid_schema_fields(self, tmp_project):
        """Generated JSON has all required Zenodo fields."""
        metadata = extract_metadata(tmp_project)
        zj = generate_zenodo_json(metadata)
        required = {"title", "upload_type", "description", "creators",
                     "license", "keywords", "access_right"}
        assert required.issubset(set(zj.keys()))
        assert zj["upload_type"] == "software"
        assert zj["access_right"] == "open"
        assert isinstance(zj["creators"], list)
        assert len(zj["creators"]) > 0

    def test_author_format(self, tmp_project):
        """Authors are in 'Last, First' format."""
        metadata = extract_metadata(tmp_project)
        zj = generate_zenodo_json(metadata)
        for creator in zj["creators"]:
            assert "name" in creator
            # Default author should be Last, First
            if "Ahmad" in creator["name"]:
                assert creator["name"].startswith("Ahmad,")

    def test_related_identifier_github(self, tmp_project):
        """Related identifiers include GitHub URL."""
        metadata = extract_metadata(tmp_project)
        zj = generate_zenodo_json(metadata)
        assert len(zj["related_identifiers"]) >= 1
        ri = zj["related_identifiers"][0]
        assert "github.com/mahmood726-cyber" in ri["identifier"]
        assert ri["relation"] == "isSupplementTo"


# ---------------------------------------------------------------------------
# 9-11. build_archive
# ---------------------------------------------------------------------------

class TestBuildArchive:

    def test_excludes_git_pycache_env(self, tmp_project):
        """Archive excludes .git, __pycache__, .env, .claude, PROGRESS.md."""
        archive_path = tmp_project / "test_output.zip"
        info = build_archive(tmp_project, archive_path)

        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()

        # These should NOT be in the archive
        for name in names:
            assert not name.startswith(".git/"), f".git/ found: {name}"
            assert not name.startswith("__pycache__/"), f"__pycache__/ found: {name}"
            assert not name.startswith(".claude/"), f".claude/ found: {name}"
            assert name != ".env", f".env found"
            assert name != "PROGRESS.md", f"PROGRESS.md found"

    def test_includes_py_html_md(self, tmp_project):
        """Archive includes .py, .html, .md files."""
        archive_path = tmp_project / "test_output.zip"
        info = build_archive(tmp_project, archive_path)

        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()

        # These should be present
        assert "main.py" in names
        assert "dashboard.html" in names
        assert "README.md" in names
        assert "LICENSE" in names
        assert info["file_count"] >= 4

    def test_skips_large_files(self, tmp_project):
        """Files > 50MB are skipped with a warning."""
        # Create a file just over 50MB in the report
        big_file = tmp_project / "huge_data.csv"
        # Don't actually write 50MB, just test the logic with a mock
        big_file.write_text("x," * 100, encoding="utf-8")

        # Temporarily lower the limit for testing
        import zenodo_publish
        original_limit = zenodo_publish.MAX_FILE_SIZE
        zenodo_publish.MAX_FILE_SIZE = 50  # 50 bytes
        try:
            archive_path = tmp_project / "test_output.zip"
            info = build_archive(tmp_project, archive_path)
            # Some files should be flagged as too large
            assert len(info["skipped_large"]) > 0
        finally:
            zenodo_publish.MAX_FILE_SIZE = original_limit

    def test_archive_is_valid_zip(self, tmp_project):
        """Output is a valid zip file."""
        archive_path = tmp_project / "test_output.zip"
        build_archive(tmp_project, archive_path)
        assert zipfile.is_zipfile(archive_path)


# ---------------------------------------------------------------------------
# 12. Dry-run
# ---------------------------------------------------------------------------

class TestDryRun:

    @patch.dict(os.environ, {}, clear=True)
    def test_dry_run_no_api_calls(self, tmp_project):
        """Dry run works without token and makes no API calls."""
        # Patch requests to detect any accidental calls
        with patch("zenodo_publish.ZenodoClient") as mock_client:
            captured = StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                dry_run_report(tmp_project, verbose=False)
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            # ZenodoClient should never be instantiated
            mock_client.assert_not_called()
            assert "DRY RUN" in output
            assert "Test Project Alpha" in output

    def test_dry_run_shows_file_counts(self, tmp_project):
        """Dry run reports file include/exclude counts."""
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            dry_run_report(tmp_project, verbose=False)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert "Files to include:" in output
        assert "Files excluded:" in output


# ---------------------------------------------------------------------------
# 13-14. ZenodoClient (mocked)
# ---------------------------------------------------------------------------

class TestZenodoClient:

    @patch("zenodo_publish.requests" if False else "builtins.__import__", side_effect=None)
    def _make_client(self, mock_import=None):
        """Helper: create a client with mocked requests."""
        pass

    @patch("zenodo_publish.ZenodoClient.__init__", return_value=None)
    def test_create_deposition_mock(self, mock_init):
        """create_deposition returns deposition_id and bucket_url."""
        client = ZenodoClient.__new__(ZenodoClient)
        client.base_url = "https://sandbox.zenodo.org"
        client.token = "fake-token"
        client._requests = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": 12345,
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket-uuid"},
        }
        mock_response.raise_for_status = MagicMock()
        client._requests.post.return_value = mock_response

        dep_id, bucket_url = client.create_deposition()
        assert dep_id == 12345
        assert "bucket-uuid" in bucket_url

    @patch("zenodo_publish.ZenodoClient.__init__", return_value=None)
    def test_publish_returns_doi(self, mock_init):
        """publish returns DOI string from API response."""
        client = ZenodoClient.__new__(ZenodoClient)
        client.base_url = "https://sandbox.zenodo.org"
        client.token = "fake-token"
        client._requests = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"doi": "10.5281/zenodo.12345"}
        mock_response.raise_for_status = MagicMock()
        client._requests.post.return_value = mock_response

        doi = client.publish(12345)
        assert doi == "10.5281/zenodo.12345"

    @patch("zenodo_publish.ZenodoClient.__init__", return_value=None)
    def test_upload_file(self, mock_init, tmp_path):
        """upload_file sends PUT with file data."""
        client = ZenodoClient.__new__(ZenodoClient)
        client.base_url = "https://sandbox.zenodo.org"
        client.token = "fake-token"
        client._requests = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"key": "test.zip"}
        mock_response.raise_for_status = MagicMock()
        client._requests.put.return_value = mock_response

        test_file = tmp_path / "test.zip"
        test_file.write_bytes(b"PK\x03\x04fake")

        result = client.upload_file("https://sandbox.zenodo.org/api/files/bucket", test_file)
        assert result["key"] == "test.zip"
        client._requests.put.assert_called_once()

    @patch("zenodo_publish.ZenodoClient.__init__", return_value=None)
    def test_update_metadata(self, mock_init):
        """update_metadata sends PUT with metadata payload."""
        client = ZenodoClient.__new__(ZenodoClient)
        client.base_url = "https://sandbox.zenodo.org"
        client.token = "fake-token"
        client._requests = MagicMock()

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 999}
        mock_response.raise_for_status = MagicMock()
        client._requests.put.return_value = mock_response

        meta = {"title": "Test", "upload_type": "software"}
        client.update_metadata(999, meta)
        client._requests.put.assert_called_once()
        call_args = client._requests.put.call_args
        assert call_args[1]["json"] == {"metadata": meta}


# ---------------------------------------------------------------------------
# 15. update_readme_with_doi
# ---------------------------------------------------------------------------

class TestUpdateReadme:

    def test_doi_badge_inserted(self, tmp_path):
        """DOI badge is inserted after the first heading."""
        readme = tmp_path / "README.md"
        readme.write_text("# My Project\n\nSome description.\n", encoding="utf-8")

        result = update_readme_with_doi(readme, "10.5281/zenodo.99999")
        assert result is True

        content = readme.read_text(encoding="utf-8")
        assert "[![DOI]" in content
        assert "10.5281/zenodo.99999" in content

    def test_no_duplicate_badge(self, tmp_path):
        """Badge is not duplicated if DOI already present."""
        readme = tmp_path / "README.md"
        readme.write_text(
            "# My Project\n\n"
            "[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.99999.svg)]"
            "(https://doi.org/10.5281/zenodo.99999)\n\n"
            "Description.\n",
            encoding="utf-8",
        )
        result = update_readme_with_doi(readme, "10.5281/zenodo.99999")
        assert result is False

    def test_update_protocol_with_doi(self, tmp_path):
        """DOI line is appended to E156-PROTOCOL.md."""
        proto = tmp_path / "E156-PROTOCOL.md"
        proto.write_text("# Protocol\n\nProject: X\n", encoding="utf-8")

        result = update_protocol_with_doi(proto, "10.5281/zenodo.88888")
        assert result is True

        content = proto.read_text(encoding="utf-8")
        assert "10.5281/zenodo.88888" in content


# ---------------------------------------------------------------------------
# 16. Batch processing
# ---------------------------------------------------------------------------

class TestBatchProcess:

    def test_finds_submission_ready_projects(self, tmp_index):
        """parse_index_md finds SUBMISSION-READY projects."""
        index_file, dir_a, dir_b, dir_c = tmp_index
        projects = parse_index_md(index_file, "SUBMISSION-READY")
        names = [p["name"] for p in projects]
        assert "ProjectA" in names
        assert "ProjectC" in names
        assert "ProjectB" not in names  # ACTIVE, not SUBMISSION-READY

    def test_batch_dry_run(self, tmp_index):
        """Batch dry-run processes matching projects without API calls."""
        index_file, dir_a, dir_b, dir_c = tmp_index

        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            summary = batch_process(
                index_file, status_filter="SUBMISSION-READY",
                sandbox=True, dry_run=True,
            )
        finally:
            sys.stdout = old_stdout

        # ProjectA and ProjectC should be processed; ProjectD is missing dir
        assert summary["processed"] >= 2
        assert summary["failed"] == 0

    def test_batch_skips_missing_dirs(self, tmp_index):
        """Batch mode skips projects whose directories don't exist."""
        index_file, *_ = tmp_index

        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            summary = batch_process(
                index_file, status_filter="SUBMISSION-READY",
                sandbox=True, dry_run=True,
            )
        finally:
            sys.stdout = old_stdout

        # ProjectD has C:\NonExistent\Path — should be skipped
        assert summary["skipped"] >= 1


# ---------------------------------------------------------------------------
# 17. Token safety
# ---------------------------------------------------------------------------

class TestTokenSafety:

    def test_no_hardcoded_tokens_in_source(self):
        """No API tokens or secrets appear in the source code."""
        source_dir = Path(__file__).parent
        for py_file in source_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            # Check for patterns that look like hardcoded tokens
            # Zenodo tokens are typically long alphanumeric strings
            lines = content.splitlines()
            for i, line in enumerate(lines, 1):
                # Skip comments and docstrings that discuss tokens
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'"):
                    continue
                # No assignment of token-like strings (>20 chars alphanumeric)
                import re
                token_assignment = re.search(
                    r'(?:token|key|secret|password)\s*=\s*["\'][A-Za-z0-9]{20,}["\']',
                    line, re.IGNORECASE,
                )
                assert token_assignment is None, (
                    f"Possible hardcoded token in {py_file.name}:{i}: {line.strip()}"
                )

    def test_token_from_env_only(self):
        """Token retrieval uses os.environ, not hardcoded values."""
        source = Path(__file__).parent / "zenodo_publish.py"
        content = source.read_text(encoding="utf-8")
        # Must reference env vars
        assert "ZENODO_TOKEN" in content
        assert "ZENODO_SANDBOX_TOKEN" in content
        assert "os.environ" in content


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_project(self, tmp_path):
        """A project with no files still produces valid metadata."""
        metadata = extract_metadata(tmp_path)
        assert metadata["title"] == tmp_path.name
        assert isinstance(metadata["authors"], list)
        assert len(metadata["authors"]) > 0

    def test_archive_empty_project(self, tmp_path):
        """An empty project produces a valid (empty) zip."""
        archive_path = tmp_path / "empty.zip"
        info = build_archive(tmp_path, archive_path)
        assert info["file_count"] == 0
        assert zipfile.is_zipfile(archive_path)

    def test_should_include_file_logic(self, tmp_path):
        """should_include_file correctly categorizes files."""
        py = tmp_path / "code.py"
        py.write_text("x=1\n", encoding="utf-8")
        assert should_include_file(py, tmp_path) is True

        env = tmp_path / ".env"
        env.write_text("SECRET=x\n", encoding="utf-8")
        assert should_include_file(env, tmp_path) is False

        pycache_dir = tmp_path / "__pycache__"
        pycache_dir.mkdir()
        pyc = pycache_dir / "code.cpython-311.pyc"
        pyc.write_bytes(b"\x00")
        assert should_include_file(pyc, tmp_path) is False

    def test_license_no_file(self, tmp_path):
        """Missing LICENSE file returns 'other-open'."""
        assert detect_license(tmp_path) == "other-open"

    def test_zenodo_json_round_trip(self, tmp_project):
        """Generated .zenodo.json is valid JSON that round-trips."""
        metadata = extract_metadata(tmp_project)
        zj = generate_zenodo_json(metadata)
        json_str = json.dumps(zj, indent=2, ensure_ascii=False)
        parsed = json.loads(json_str)
        assert parsed["title"] == zj["title"]
        assert parsed["creators"] == zj["creators"]
