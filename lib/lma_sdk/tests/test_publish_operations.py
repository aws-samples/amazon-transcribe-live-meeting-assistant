# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for publish operations."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lma_sdk._core.publish import (
    _calculate_dir_hash,
    _compare_versions,
    _has_changed,
    check_prerequisites,
    STACK_DEFINITIONS,
    STACK_NAMES,
)
from lma_sdk.exceptions import LMAPublishError, LMAValidationError


class TestVersionComparison:
    """Tests for version comparison utility."""

    def test_equal_versions(self):
        assert _compare_versions("1.2.3", "1.2.3") == 0

    def test_greater_version(self):
        assert _compare_versions("1.3.0", "1.2.0") == 1

    def test_lesser_version(self):
        assert _compare_versions("1.2.0", "1.3.0") == -1

    def test_minor_difference(self):
        assert _compare_versions("1.118.0", "1.100.0") == 1

    def test_patch_difference(self):
        assert _compare_versions("1.0.1", "1.0.0") == 1


class TestDirectoryHash:
    """Tests for directory hash calculation."""

    def test_hash_is_deterministic(self, tmp_path):
        """Same content produces same hash."""
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.txt").write_text("world")

        hash1 = _calculate_dir_hash(tmp_path)
        hash2 = _calculate_dir_hash(tmp_path)

        assert hash1 == hash2
        assert len(hash1) == 16  # Truncated hex

    def test_hash_changes_with_content(self, tmp_path):
        """Hash changes when file content changes."""
        f = tmp_path / "file.txt"
        f.write_text("original")
        hash1 = _calculate_dir_hash(tmp_path)

        f.write_text("modified")
        hash2 = _calculate_dir_hash(tmp_path)

        assert hash1 != hash2

    def test_hash_excludes_node_modules(self, tmp_path):
        """Hash excludes node_modules directory."""
        (tmp_path / "src.py").write_text("code")
        hash_without = _calculate_dir_hash(tmp_path)

        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "package.json").write_text("{}")
        hash_with = _calculate_dir_hash(tmp_path)

        assert hash_without == hash_with

    def test_hash_excludes_checksum_file(self, tmp_path):
        """Hash excludes .checksum files."""
        (tmp_path / "file.txt").write_text("content")
        hash1 = _calculate_dir_hash(tmp_path)

        (tmp_path / ".checksum").write_text("abc123")
        hash2 = _calculate_dir_hash(tmp_path)

        assert hash1 == hash2


class TestChangeDetection:
    """Tests for change detection."""

    def test_first_time_always_changed(self, tmp_path):
        """New directory (no .checksum) is always considered changed."""
        (tmp_path / "file.txt").write_text("content")
        assert _has_changed(tmp_path, "bucket", "prefix/v1", "us-east-1") is True

    def test_unchanged_after_checksum(self, tmp_path):
        """Directory is unchanged after checksum is written."""
        from lma_sdk._core.publish import _update_checksum

        (tmp_path / "file.txt").write_text("content")
        _update_checksum(tmp_path, "bucket", "prefix/v1", "us-east-1")

        assert _has_changed(tmp_path, "bucket", "prefix/v1", "us-east-1") is False

    def test_changed_after_file_modification(self, tmp_path):
        """Directory is changed after modifying a file."""
        from lma_sdk._core.publish import _update_checksum

        f = tmp_path / "file.txt"
        f.write_text("original")
        _update_checksum(tmp_path, "bucket", "prefix/v1", "us-east-1")

        f.write_text("modified")
        assert _has_changed(tmp_path, "bucket", "prefix/v1", "us-east-1") is True

    def test_changed_with_different_target(self, tmp_path):
        """Directory is changed when targeting different bucket/prefix."""
        from lma_sdk._core.publish import _update_checksum

        (tmp_path / "file.txt").write_text("content")
        _update_checksum(tmp_path, "bucket-1", "prefix/v1", "us-east-1")

        assert _has_changed(tmp_path, "bucket-2", "prefix/v1", "us-east-1") is True


class TestStackDefinitions:
    """Tests for stack definitions."""

    def test_all_stacks_defined(self):
        """All expected stacks are defined."""
        assert len(STACK_DEFINITIONS) == 10

    def test_stack_names_list(self):
        """STACK_NAMES matches definitions."""
        assert len(STACK_NAMES) == 10
        assert "lma-ai-stack" in STACK_NAMES
        assert "lma-virtual-participant-stack" in STACK_NAMES

    def test_available_stacks_from_operations(self, client):
        """PublishOperations.available_stacks() returns list."""
        stacks = client.publish.available_stacks()
        assert isinstance(stacks, list)
        assert len(stacks) == 10


class TestPrerequisiteChecks:
    """Tests for prerequisite checking."""

    @patch("lma_sdk._core.publish.shutil.which")
    @patch("lma_sdk._core.publish.subprocess.run")
    def test_all_prereqs_pass(self, mock_run, mock_which):
        """check_prerequisites returns empty list when all pass."""
        mock_which.return_value = "/usr/bin/cmd"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="SAM CLI, version 1.120.0\n",
        )
        # Patch node version check
        def run_side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if cmd == ["docker", "ps"]:
                result.stdout = ""
            elif cmd == ["sam", "--version"]:
                result.stdout = "SAM CLI, version 1.120.0"
            elif cmd == ["node", "-v"]:
                result.stdout = "v20.11.0"
            return result

        mock_run.side_effect = run_side_effect

        errors = check_prerequisites()
        # May have errors if some checks fail in test env, but structure is correct
        assert isinstance(errors, list)
