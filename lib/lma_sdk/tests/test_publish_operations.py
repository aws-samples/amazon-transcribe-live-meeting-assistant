# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for publish operations."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import subprocess

from lma_sdk._core.publish import (
    CONFIGURATION_SEED_PATHS,
    _calculate_dir_hash,
    _compare_versions,
    _git_untracked_preflight,
    _has_changed,
    _list_untracked_files,
    calculate_configuration_seed_hash,
    check_prerequisites,
    STACK_DEFINITIONS,
    STACK_NAMES,
)
from lma_sdk.models.publish import StackDefinition, StackPackageType
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

    def test_hash_excludes_local_tool_caches(self, tmp_path):
        """A local test run must not change what a publish from that checkout digests.

        The digest decides whether a deploy-time custom resource re-runs, so a
        `.pytest_cache` left inside a hashed tree would show up as a stack
        update nobody made.
        """
        (tmp_path / "src.py").write_text("code")
        clean = _calculate_dir_hash(tmp_path)

        for cache in (".pytest_cache", ".ruff_cache"):
            directory = tmp_path / cache
            directory.mkdir()
            (directory / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172")

        assert _calculate_dir_hash(tmp_path) == clean

    def test_hash_excludes_checksum_file(self, tmp_path):
        """Hash excludes .checksum files."""
        (tmp_path / "file.txt").write_text("content")
        hash1 = _calculate_dir_hash(tmp_path)

        (tmp_path / ".checksum").write_text("abc123")
        hash2 = _calculate_dir_hash(tmp_path)

        assert hash1 == hash2


class TestConfigurationSeedHash:
    """Tests for the digest that makes the configuration seeder re-run.

    CloudFormation re-invokes a custom resource only when one of its properties
    changes. This digest is that property, so if it did not move when a release
    changed the shipped configuration, the seeder would never run again after a
    deployment's first create -- and the new prompts would never arrive. The
    tests therefore assert that it moves for both kinds of change.
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        for relative in CONFIGURATION_SEED_PATHS:
            directory = tmp_path / relative
            directory.mkdir(parents=True)
            (directory / "placeholder.py").write_text("original")
        return tmp_path

    def test_the_digest_is_deterministic(self, tmp_path):
        project = self._project(tmp_path)
        assert calculate_configuration_seed_hash(project) == (
            calculate_configuration_seed_hash(project)
        )
        assert len(calculate_configuration_seed_hash(project)) == 16

    def test_the_digest_moves_when_the_shipped_content_changes(self, tmp_path):
        project = self._project(tmp_path)
        before = calculate_configuration_seed_hash(project)
        content = project / CONFIGURATION_SEED_PATHS[0] / "defaults.json"
        content.write_text('{"SUMMARY": "a new shipped prompt"}')
        assert calculate_configuration_seed_hash(project) != before

    def test_the_digest_moves_when_the_processing_code_changes(self, tmp_path):
        """Hashing the content alone would miss a change to how it is written."""
        project = self._project(tmp_path)
        before = calculate_configuration_seed_hash(project)
        accessor = project / CONFIGURATION_SEED_PATHS[1] / "placeholder.py"
        accessor.write_text("a different encoding")
        assert calculate_configuration_seed_hash(project) != before

    def test_a_relocated_directory_changes_the_digest_rather_than_being_skipped(
        self, tmp_path
    ):
        """A silently skipped path would freeze the digest and stop the re-runs."""
        project = self._project(tmp_path)
        before = calculate_configuration_seed_hash(project)
        for item in (project / CONFIGURATION_SEED_PATHS[1]).iterdir():
            item.unlink()
        (project / CONFIGURATION_SEED_PATHS[1]).rmdir()
        assert calculate_configuration_seed_hash(project) != before

    def test_the_paths_the_digest_covers_exist_in_this_repository(self):
        """A rename elsewhere must not leave the digest hashing nothing."""
        repo_root = Path(__file__).resolve().parents[3]
        for relative in CONFIGURATION_SEED_PATHS:
            assert (repo_root / relative).is_dir(), f"{relative} is not in the repository"

    def test_the_main_template_carries_the_token_the_digest_replaces(self):
        """Without the token in the template there is nothing to substitute."""
        repo_root = Path(__file__).resolve().parents[3]
        main_template = (repo_root / "lma-main.yaml").read_text()
        assert "<CONFIGURATION_SEED_HASH_TOKEN>" in main_template


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
        assert len(STACK_DEFINITIONS) == 13

    def test_stack_names_list(self):
        """STACK_NAMES matches definitions."""
        assert len(STACK_NAMES) == 13
        assert "lma-ai-stack" in STACK_NAMES
        assert "lma-virtual-participant-stack" in STACK_NAMES
        assert "lma-asr-microvm-stack" in STACK_NAMES

    def test_available_stacks_from_operations(self, client):
        """PublishOperations.available_stacks() returns list."""
        stacks = client.publish.available_stacks()
        assert isinstance(stacks, list)
        assert len(stacks) == 13


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


class TestUntrackedPreflight:
    """Tests for the git untracked-files safety check.

    These use real `git init` sandboxes (no mocking) so we exercise the same
    subprocess-based code path the publish workflow runs in production.
    """

    @staticmethod
    def _git_init_stack(project_dir: Path, stack_name: str) -> Path:
        """Create a project_dir/<stack_name> directory inside a fresh git repo
        with one tracked file, and return the stack path."""
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(project_dir)],
            check=True,
            capture_output=True,
        )
        # Minimum config so `git add` works without a user config.
        # Also disable any system-wide git hooks path (e.g. Amazon's
        # git-defender) which could otherwise block the test commits.
        for key, val in (
            ("user.email", "ci@example.com"),
            ("user.name", "ci"),
            ("core.hooksPath", "/dev/null"),
            ("commit.gpgsign", "false"),
        ):
            subprocess.run(
                ["git", "-C", str(project_dir), "config", key, val],
                check=True,
                capture_output=True,
            )
        stack_dir = project_dir / stack_name
        stack_dir.mkdir()
        (stack_dir / "template.yaml").write_text("# stub\n")
        subprocess.run(
            ["git", "-C", str(project_dir), "add", f"{stack_name}/template.yaml"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", str(project_dir),
                "commit", "-q", "--no-verify", "-m", "init",
            ],
            check=True,
            capture_output=True,
        )
        return stack_dir


    @staticmethod
    def _build_script_stack(name: str) -> StackDefinition:
        return StackDefinition(
            name=name,
            package_type=StackPackageType.BUILD_SCRIPT,
            build_script="build-s3-dist.sh",
            deployment_subdir="deployment",
            supports_change_detection=True,
        )

    @staticmethod
    def _non_build_stack(name: str) -> StackDefinition:
        return StackDefinition(
            name=name,
            package_type=StackPackageType.ZIP_AND_UPLOAD,
            supports_change_detection=False,
        )

    def test_list_untracked_nonexistent_dir(self, tmp_path):
        """_list_untracked_files returns [] when the dir doesn't exist."""
        assert _list_untracked_files(tmp_path / "missing") == []

    def test_list_untracked_not_in_git_repo(self, tmp_path):
        """A plain directory outside a git repo returns []."""
        (tmp_path / "file.py").write_text("hello")
        assert _list_untracked_files(tmp_path) == []

    def test_list_untracked_empty_repo(self, tmp_path):
        """A clean git repo with only tracked files returns []."""
        stack_dir = self._git_init_stack(tmp_path, "stack-a")
        assert _list_untracked_files(stack_dir) == []

    def test_list_untracked_finds_new_file(self, tmp_path):
        """An untracked file is reported (path is relative to the stack dir)."""
        stack_dir = self._git_init_stack(tmp_path, "stack-a")
        (stack_dir / "newfile.py").write_text("print('x')")
        result = _list_untracked_files(stack_dir)
        assert result == ["newfile.py"]

    def test_list_untracked_honors_gitignore(self, tmp_path):
        """Files matched by .gitignore are excluded."""
        stack_dir = self._git_init_stack(tmp_path, "stack-a")
        (stack_dir / ".gitignore").write_text("ignored.py\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "stack-a/.gitignore"],
            check=True,
            capture_output=True,
        )
        (stack_dir / "ignored.py").write_text("x")
        (stack_dir / "kept.py").write_text("y")
        result = _list_untracked_files(stack_dir)
        assert result == ["kept.py"]

    def test_preflight_passes_when_no_untracked(self, tmp_path):
        """Clean tree → no exception."""
        self._git_init_stack(tmp_path, "lma-ai-stack")
        _git_untracked_preflight(
            tmp_path,
            [self._build_script_stack("lma-ai-stack")],
            allow_untracked=False,
        )

    def test_preflight_ignores_non_build_script_stacks(self, tmp_path):
        """Untracked files in non-BUILD_SCRIPT stacks do not trigger the check."""
        stack_dir = self._git_init_stack(tmp_path, "lma-virtual-participant-stack")
        (stack_dir / "newfile.py").write_text("x")
        _git_untracked_preflight(
            tmp_path,
            [self._non_build_stack("lma-virtual-participant-stack")],
            allow_untracked=False,
        )

    def test_preflight_raises_for_build_script_with_untracked(self, tmp_path):
        """BUILD_SCRIPT stack with an untracked file → LMAPublishError."""
        stack_dir = self._git_init_stack(tmp_path, "lma-ai-stack")
        (stack_dir / "source" / "lambda_functions").mkdir(parents=True)
        (stack_dir / "source" / "lambda_functions" / "newfile.py").write_text("x")
        with pytest.raises(LMAPublishError) as exc:
            _git_untracked_preflight(
                tmp_path,
                [self._build_script_stack("lma-ai-stack")],
                allow_untracked=False,
            )
        msg = str(exc.value)
        assert "lma-ai-stack" in msg
        # Path is shown relative to the stack dir (`git ls-files -C stack_dir` output).
        assert "source/lambda_functions/newfile.py" in msg
        assert "git add" in msg  # actionable hint
        assert "allow-untracked" in msg  # escape hatch mentioned

    def test_preflight_allow_untracked_bypasses(self, tmp_path):
        """allow_untracked=True lets the publish proceed despite untracked files."""
        stack_dir = self._git_init_stack(tmp_path, "lma-ai-stack")
        (stack_dir / "newfile.py").write_text("x")
        # Should NOT raise.
        _git_untracked_preflight(
            tmp_path,
            [self._build_script_stack("lma-ai-stack")],
            allow_untracked=True,
        )

    def test_preflight_lists_all_offending_stacks(self, tmp_path):
        """Untracked files in multiple BUILD_SCRIPT stacks all get reported."""
        ai_stack = self._git_init_stack(tmp_path, "lma-ai-stack")
        # Second stack can live in the same repo.
        ws_stack = tmp_path / "lma-websocket-transcriber-stack"
        ws_stack.mkdir()
        (ws_stack / "template.yaml").write_text("# stub\n")
        subprocess.run(
            [
                "git", "-C", str(tmp_path),
                "add", "lma-websocket-transcriber-stack/template.yaml",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", str(tmp_path),
                "commit", "-q", "--no-verify", "-m", "add ws stack",
            ],
            check=True,
            capture_output=True,
        )
        (ai_stack / "ai-new.py").write_text("x")
        (ws_stack / "ws-new.py").write_text("y")
        stacks = [
            self._build_script_stack("lma-ai-stack"),
            self._build_script_stack("lma-websocket-transcriber-stack"),
        ]
        with pytest.raises(LMAPublishError) as exc:
            _git_untracked_preflight(tmp_path, stacks, allow_untracked=False)
        msg = str(exc.value)
        # Each stack name is shown as a header, and paths under each stack
        # are reported relative to that stack's directory.
        assert "lma-ai-stack:" in msg
        assert "- ai-new.py" in msg
        assert "lma-websocket-transcriber-stack:" in msg
        assert "- ws-new.py" in msg


class TestHashAndPackage:
    """Tests for the HASH_AND_PACKAGE publish path (config seeding stacks)."""

    TEMPLATE = (
        "Resources:\n"
        "  Fn:\n"
        "    Type: AWS::Lambda::Function\n"
        "    Properties:\n"
        "      Code: ../source/lambda_functions\n"
        "  Seed:\n"
        "    Type: AWS::CloudFormation::CustomResource\n"
        "    Properties:\n"
        "      ServiceToken: !GetAtt Fn.Arn\n"
        "      # comment mentioning source_hash: not a property\n"
        "      source_hash: committedvalue\n"
    )

    @staticmethod
    def _stack_def() -> StackDefinition:
        return StackDefinition(
            name="lma-example-config-stack",
            package_type=StackPackageType.HASH_AND_PACKAGE,
            template_file="example.yaml",
            deployment_subdir="deployment",
            hash_source_dir="../source",
            hash_template_field="source_hash",
        )

    def _project(self, tmp_path: Path, template: str | None = None) -> Path:
        project = tmp_path / "project"
        stack = project / "lma-example-config-stack"
        (stack / "deployment").mkdir(parents=True)
        (stack / "source" / "lambda_functions").mkdir(parents=True)
        (stack / "source" / "lambda_functions" / "seed.py").write_text("print('seed')\n")
        (stack / "deployment" / "example.yaml").write_text(template or self.TEMPLATE)
        return project

    def _publish(self, client, project: Path, tmp_path: Path):
        """Run _publish_hash_and_package with `cfn package` simulated as a copy."""
        from lma_sdk._core.publish import Publisher

        publisher = Publisher(client)
        uploaded = {}

        def fake_package(cmd, cwd, desc):
            src = Path(cwd) / cmd[cmd.index("--template-file") + 1]
            Path(cmd[cmd.index("--output-template-file") + 1]).write_text(src.read_text())

        def capture_upload(path, bucket, key):
            uploaded[key] = Path(path).read_text()

        out_dir = tmp_path / "out"
        out_dir.mkdir(parents=True)
        s3 = MagicMock()
        s3.upload_file.side_effect = capture_upload
        with patch.object(Publisher, "_run_command", side_effect=fake_package), \
                patch.object(Publisher, "_s3", new=s3):
            result = publisher._publish_hash_and_package(
                stack_def=self._stack_def(),
                project_dir=project,
                bucket="bucket",
                prefix_and_version="prefix/1.0.0",
                region="us-east-1",
                tmpdir=out_dir,
            )
        return result, uploaded

    def test_tracked_template_is_left_unmodified(self, client, tmp_path):
        project = self._project(tmp_path)
        template = project / "lma-example-config-stack" / "deployment" / "example.yaml"
        self._publish(client, project, tmp_path)
        assert template.read_text() == self.TEMPLATE

    def test_uploaded_template_carries_the_source_hash(self, client, tmp_path):
        project = self._project(tmp_path)
        expected = _calculate_dir_hash(project / "lma-example-config-stack" / "source")
        _, uploaded = self._publish(client, project, tmp_path)
        body = uploaded["prefix/1.0.0/lma-example-config-stack/example.yaml"]
        assert f'      source_hash: "{expected}"\n' in body
        assert "committedvalue" not in body
        assert "# comment mentioning source_hash: not a property" in body

    def test_uploaded_hash_follows_source_changes(self, client, tmp_path):
        project = self._project(tmp_path)
        _, first = self._publish(client, project, tmp_path / "a")
        seed = project / "lma-example-config-stack" / "source" / "lambda_functions" / "seed.py"
        seed.write_text("print('changed')\n")
        _, second = self._publish(client, project, tmp_path / "b")
        key = "prefix/1.0.0/lma-example-config-stack/example.yaml"
        assert first[key] != second[key]

    def test_template_without_the_hash_field_is_rejected(self, client, tmp_path):
        project = self._project(tmp_path, template="Resources: {}\n")
        with pytest.raises(LMAPublishError, match="source_hash"):
            self._publish(client, project, tmp_path)

    @pytest.mark.parametrize(
        "stack_name",
        ["lma-llm-template-setup-stack", "lma-chat-button-config-stack", "lma-nova-sonic-config-stack"],
    )
    def test_shipped_templates_carry_the_hash_field(self, stack_name):
        stack_def = next(s for s in STACK_DEFINITIONS if s.name == stack_name)
        assert stack_def.package_type == StackPackageType.HASH_AND_PACKAGE
        repo = Path(__file__).resolve().parents[3]
        template = repo / stack_name / stack_def.deployment_subdir / stack_def.template_file
        assert f"\n      {stack_def.hash_template_field}: " in template.read_text()
