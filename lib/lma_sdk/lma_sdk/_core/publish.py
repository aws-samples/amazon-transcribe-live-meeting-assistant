# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Core publish operations — build and upload LMA artifacts to S3.

This is a Python port of the original publish.sh bash script, providing
programmatic access to the full LMA publish workflow with Rich progress
reporting, change detection, and per-stack control.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import boto3
import botocore.exceptions

from lma_sdk.exceptions import LMAPublishError, LMAValidationError
from lma_sdk.models.publish import (
    PublishConfig,
    PublishResult,
    StackDefinition,
    StackPackageType,
    StackPublishResult,
)

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Stack definitions — describes how each LMA sub-stack is packaged
# ──────────────────────────────────────────────────────────────

STACK_DEFINITIONS: list[StackDefinition] = [
    StackDefinition(
        name="lma-browser-extension-stack",
        package_type=StackPackageType.ZIP_WITH_TOKEN_REPLACE,
        template_file="template.yaml",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-virtual-participant-stack",
        package_type=StackPackageType.ZIP_AND_UPLOAD,
        template_file="template.yaml",
        supports_change_detection=False,  # Always publish (fast)
    ),
    StackDefinition(
        name="lma-vpc-stack",
        package_type=StackPackageType.CFN_PACKAGE,
        template_file="template.yaml",
        supports_change_detection=False,
    ),
    StackDefinition(
        name="lma-cognito-stack",
        package_type=StackPackageType.CFN_PACKAGE,
        template_file="lma-cognito-stack.yaml",
        deployment_subdir="deployment",
        supports_change_detection=False,
    ),
    StackDefinition(
        name="lma-meetingassist-setup-stack",
        package_type=StackPackageType.DELEGATE_SCRIPT,
        delegate_script="publish.sh",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-bedrockkb-stack",
        package_type=StackPackageType.DELEGATE_SCRIPT,
        delegate_script="publish.sh",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-websocket-transcriber-stack",
        package_type=StackPackageType.BUILD_SCRIPT,
        build_script="build-s3-dist.sh",
        deployment_subdir="deployment",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-ai-stack",
        package_type=StackPackageType.BUILD_SCRIPT,
        build_script="build-s3-dist.sh",
        deployment_subdir="deployment",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-llm-template-setup-stack",
        package_type=StackPackageType.HASH_AND_PACKAGE,
        template_file="llm-template-setup.yaml",
        deployment_subdir="deployment",
        hash_source_dir="../source",
        hash_template_field="source_hash",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-chat-button-config-stack",
        package_type=StackPackageType.HASH_AND_PACKAGE,
        template_file="chat-button-config.yaml",
        deployment_subdir="deployment",
        hash_source_dir="../source",
        hash_template_field="source_hash",
        supports_change_detection=True,
    ),
    StackDefinition(
        name="lma-nova-sonic-config-stack",
        package_type=StackPackageType.HASH_AND_PACKAGE,
        template_file="nova-sonic-config.yaml",
        deployment_subdir="deployment",
        hash_source_dir="../source",
        hash_template_field="source_hash",
        s3_template_path="deployment/nova-sonic-config.yaml",
        supports_change_detection=True,
    ),
]

STACK_NAMES = [s.name for s in STACK_DEFINITIONS]


# ──────────────────────────────────────────────────────────────
# Prerequisite checks
# ──────────────────────────────────────────────────────────────

def _check_command(cmd: str, install_hint: str) -> str | None:
    """Check if a command is available. Returns error message or None."""
    if shutil.which(cmd) is None:
        return f"'{cmd}' is not installed. {install_hint}"
    return None


def _check_docker_running() -> str | None:
    """Check docker is installed and running."""
    err = _check_command("docker", "Install: https://docs.docker.com/engine/install/")
    if err:
        return err
    result = subprocess.run(["docker", "ps"], capture_output=True, timeout=10)
    if result.returncode != 0:
        return "Docker is not running. Please start Docker."
    return None


def _check_sam_version(min_version: str = "1.118.0") -> str | None:
    """Check SAM CLI version meets minimum."""
    err = _check_command(
        "sam",
        "Install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html",
    )
    if err:
        return err
    result = subprocess.run(["sam", "--version"], capture_output=True, text=True, timeout=10)
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
    if not match:
        return "Could not determine SAM CLI version."
    installed = match.group(1)
    if _compare_versions(installed, min_version) < 0:
        return f"SAM CLI version >= {min_version} required (installed: {installed})."
    return None


def _check_node_version() -> str | None:
    """Check Node.js version is 18, 20, or 22."""
    err = _check_command("node", "Install Node.js 18, 20, or 22.")
    if err:
        return err
    result = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=10)
    match = re.match(r"v(\d+)\.", result.stdout.strip())
    if not match:
        return "Could not determine Node.js version."
    major = int(match.group(1))
    if major not in (18, 20, 22):
        return f"Node.js 18, 20, or 22 required (installed: v{major})."
    return None


def _compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings. Returns -1, 0, or 1."""
    parts1 = [int(x) for x in v1.split(".")]
    parts2 = [int(x) for x in v2.split(".")]
    for a, b in zip(parts1, parts2):
        if a < b:
            return -1
        if a > b:
            return 1
    return 0


def check_prerequisites() -> list[str]:
    """Check all publish prerequisites. Returns list of error messages (empty = all OK)."""
    errors = []
    for check in [
        _check_docker_running,
        _check_sam_version,
        lambda: _check_command("zip", "Install zip."),
        lambda: _check_command("pip3", "Install pip3."),
        lambda: _check_command("npm", "Install npm."),
        _check_node_version,
    ]:
        err = check()
        if err:
            errors.append(err)
    return errors


# ──────────────────────────────────────────────────────────────
# Change detection (SHA256-based, same logic as publish.sh)
# ──────────────────────────────────────────────────────────────

def _calculate_dir_hash(directory: str | Path) -> str:
    """Calculate a SHA256 hash of directory contents (excluding node_modules, build)."""
    directory = Path(directory)
    hasher = hashlib.sha256()
    skip_dirs = {"node_modules", "build", "dist", ".aws-sam", "__pycache__", ".git"}

    files = []
    for root, dirs, filenames in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in sorted(filenames):
            if fname == ".checksum":
                continue
            fpath = Path(root) / fname
            files.append(fpath)

    files.sort()
    for fpath in files:
        rel = fpath.relative_to(directory)
        hasher.update(str(rel).encode())
        try:
            hasher.update(fpath.read_bytes())
        except OSError:
            pass

    return hasher.hexdigest()[:16]


def _has_changed(
    stack_dir: Path,
    bucket: str,
    prefix_and_version: str,
    region: str,
) -> bool:
    """Check if a stack directory has changed since last publish."""
    checksum_file = stack_dir / ".checksum"
    dir_hash = _calculate_dir_hash(stack_dir)
    combined = f"{bucket} {prefix_and_version} {region} {dir_hash}"
    current_checksum = hashlib.sha256(combined.encode()).hexdigest()

    if checksum_file.exists():
        previous_checksum = checksum_file.read_text().strip()
        if current_checksum == previous_checksum:
            return False

    return True


def _update_checksum(
    stack_dir: Path,
    bucket: str,
    prefix_and_version: str,
    region: str,
) -> None:
    """Save the current checksum for a stack directory."""
    checksum_file = stack_dir / ".checksum"
    dir_hash = _calculate_dir_hash(stack_dir)
    combined = f"{bucket} {prefix_and_version} {region} {dir_hash}"
    current_checksum = hashlib.sha256(combined.encode()).hexdigest()
    checksum_file.write_text(current_checksum + "\n")


# ──────────────────────────────────────────────────────────────
# Publisher — main publish orchestrator
# ──────────────────────────────────────────────────────────────

class Publisher:
    """Orchestrates building and publishing LMA artifacts to S3."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client

    @property
    def _s3(self):
        return self._client.session.client("s3", region_name=self._client.region)

    @property
    def _cfn(self):
        return self._client.session.client("cloudformation", region_name=self._client.region)

    def publish(
        self,
        config: PublishConfig,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> PublishResult:
        """Execute the full publish workflow.

        Args:
            config: Publish configuration.
            progress_callback: Optional callback(stack_name, message) for progress.

        Returns:
            PublishResult with outcomes for each stack.
        """
        start_time = time.time()

        # Resolve project directory
        project_dir = Path(config.project_dir).resolve()
        if not (project_dir / "lma-main.yaml").exists():
            raise LMAPublishError(
                f"Not an LMA project directory: {project_dir} (lma-main.yaml not found)"
            )

        # Resolve version
        version = config.version
        if not version:
            version_file = project_dir / "VERSION"
            if version_file.exists():
                version = version_file.read_text().strip()
            else:
                raise LMAPublishError("VERSION file not found and no version specified.")

        # Resolve bucket and prefix
        bucket = f"{config.bucket_basename}-{config.region}"
        prefix = config.prefix.rstrip("/")
        prefix_and_version = f"{prefix}/{version}"
        region = config.region

        # Check prerequisites
        if progress_callback:
            progress_callback("prerequisites", "Checking prerequisites...")
        errors = check_prerequisites()
        if errors:
            raise LMAPublishError(
                "Prerequisites not met:\n" + "\n".join(f"  • {e}" for e in errors)
            )

        # Ensure S3 bucket exists
        if progress_callback:
            progress_callback("s3", f"Ensuring bucket '{bucket}' exists...")
        self._ensure_bucket(bucket, region)

        # Create temp directory
        tmpdir = Path(tempfile.mkdtemp(prefix="lma-publish-"))
        logger.info("Using temp dir: %s", tmpdir)

        # Determine which stacks to publish
        if config.stacks:
            # Validate requested stack names
            valid_names = {s.name for s in STACK_DEFINITIONS}
            invalid = set(config.stacks) - valid_names
            if invalid:
                raise LMAValidationError(
                    f"Unknown stack(s): {', '.join(sorted(invalid))}. "
                    f"Valid stacks: {', '.join(sorted(valid_names))}"
                )
            stacks_to_publish = [s for s in STACK_DEFINITIONS if s.name in config.stacks]
        else:
            stacks_to_publish = STACK_DEFINITIONS

        # Track S3 locations for main template substitution
        vp_src_s3_location = ""
        browser_ext_src_s3_location = ""

        # Publish each stack
        stack_results: list[StackPublishResult] = []
        for stack_def in stacks_to_publish:
            stack_start = time.time()
            stack_dir = project_dir / stack_def.name

            if not stack_dir.exists():
                stack_results.append(StackPublishResult(
                    stack_name=stack_def.name,
                    success=False,
                    message=f"Directory not found: {stack_dir}",
                ))
                continue

            # Change detection
            if (
                stack_def.supports_change_detection
                and not config.force
                and not _has_changed(stack_dir, bucket, prefix_and_version, region)
            ):
                # Even when skipped, compute S3 locations needed for main template tokens
                if stack_def.name == "lma-browser-extension-stack":
                    content_hash = _calculate_dir_hash(stack_dir)
                    zip_filename = f"src-{content_hash}.zip"
                    s3_zip_key = f"{prefix_and_version}/{stack_def.name}/{zip_filename}"
                    browser_ext_src_s3_location = f"{bucket}/{s3_zip_key}"

                if progress_callback:
                    progress_callback(stack_def.name, "Skipped (unchanged)")
                stack_results.append(StackPublishResult(
                    stack_name=stack_def.name,
                    success=True,
                    skipped=True,
                    message="Unchanged since last publish",
                ))
                continue

            if progress_callback:
                progress_callback(stack_def.name, "Packaging...")

            try:
                result = self._publish_stack(
                    stack_def=stack_def,
                    project_dir=project_dir,
                    bucket=bucket,
                    bucket_basename=config.bucket_basename,
                    prefix_and_version=prefix_and_version,
                    region=region,
                    version=version,
                    tmpdir=tmpdir,
                )

                # Capture source locations for main template
                if stack_def.name == "lma-virtual-participant-stack" and result.get("vp_src_s3_location"):
                    vp_src_s3_location = result["vp_src_s3_location"]
                if stack_def.name == "lma-browser-extension-stack" and result.get("browser_ext_src_s3_location"):
                    browser_ext_src_s3_location = result["browser_ext_src_s3_location"]

                duration = time.time() - stack_start
                stack_results.append(StackPublishResult(
                    stack_name=stack_def.name,
                    success=True,
                    message=result.get("message", "Published successfully"),
                    s3_template_url=result.get("s3_template_url", ""),
                    duration_seconds=round(duration, 1),
                ))

                # Update checksum on success
                if stack_def.supports_change_detection:
                    _update_checksum(stack_dir, bucket, prefix_and_version, region)

                if progress_callback:
                    progress_callback(stack_def.name, f"Done ({duration:.1f}s)")

            except Exception as e:
                duration = time.time() - stack_start
                logger.error("Failed to publish %s: %s", stack_def.name, e)
                stack_results.append(StackPublishResult(
                    stack_name=stack_def.name,
                    success=False,
                    message=str(e),
                    duration_seconds=round(duration, 1),
                ))
                if progress_callback:
                    progress_callback(stack_def.name, f"FAILED: {e}")

        # Package main template
        if progress_callback:
            progress_callback("lma-main", "Packaging main template...")

        main_result = self._publish_main_template(
            project_dir=project_dir,
            bucket=bucket,
            prefix=prefix,
            prefix_and_version=prefix_and_version,
            region=region,
            version=version,
            vp_src_s3_location=vp_src_s3_location,
            browser_ext_src_s3_location=browser_ext_src_s3_location,
            tmpdir=tmpdir,
        )

        # Set public ACLs if requested
        if config.public:
            if progress_callback:
                progress_callback("acl", "Setting public read ACLs...")
            self._set_public_acls(bucket, prefix_and_version, prefix)

        # Cleanup
        shutil.rmtree(tmpdir, ignore_errors=True)

        total_duration = time.time() - start_time
        all_success = all(r.success for r in stack_results) and main_result["success"]
        template_url = main_result.get("template_url", "")
        console_url = (
            f"https://{region}.console.aws.amazon.com/cloudformation/home"
            f"?region={region}#/stacks/create/review"
            f"?templateURL={template_url}&stackName=LMA"
        )
        cli_command = (
            f"aws cloudformation deploy --region {region} "
            f"--template-file {tmpdir / 'lma-main.yaml'} "
            f"--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND "
            f"--stack-name LMA --parameter-overrides AdminEmail='jdoe+admin@example.com'"
        )

        return PublishResult(
            success=all_success,
            bucket=bucket,
            prefix=prefix_and_version,
            region=region,
            version=version,
            template_url=template_url,
            console_url=console_url,
            cli_deploy_command=cli_command,
            stack_results=stack_results,
            duration_seconds=round(total_duration, 1),
            message="Publish completed successfully" if all_success else "Publish completed with errors",
        )

    # ── S3 helpers ────────────────────────────────────────────

    def _ensure_bucket(self, bucket: str, region: str) -> None:
        """Create S3 bucket if it doesn't exist."""
        try:
            self._s3.head_bucket(Bucket=bucket)
            logger.info("Using existing bucket: %s", bucket)
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                logger.info("Creating S3 bucket: %s", bucket)
                create_kwargs: dict[str, Any] = {"Bucket": bucket}
                if region != "us-east-1":
                    create_kwargs["CreateBucketConfiguration"] = {
                        "LocationConstraint": region
                    }
                self._s3.create_bucket(**create_kwargs)
                self._s3.put_bucket_versioning(
                    Bucket=bucket,
                    VersioningConfiguration={"Status": "Enabled"},
                )
            else:
                raise LMAPublishError(f"Cannot access S3 bucket '{bucket}': {e}") from e

    # ── Per-stack publish handlers ────────────────────────────

    def _publish_stack(
        self,
        stack_def: StackDefinition,
        project_dir: Path,
        bucket: str,
        bucket_basename: str,
        prefix_and_version: str,
        region: str,
        version: str,
        tmpdir: Path,
    ) -> dict[str, Any]:
        """Publish a single stack based on its definition."""
        handlers = {
            StackPackageType.ZIP_AND_UPLOAD: self._publish_zip_and_upload,
            StackPackageType.ZIP_WITH_TOKEN_REPLACE: self._publish_zip_with_token_replace,
            StackPackageType.CFN_PACKAGE: self._publish_cfn_package,
            StackPackageType.DELEGATE_SCRIPT: self._publish_delegate_script,
            StackPackageType.BUILD_SCRIPT: self._publish_build_script,
            StackPackageType.HASH_AND_PACKAGE: self._publish_hash_and_package,
        }
        handler = handlers.get(stack_def.package_type)
        if not handler:
            raise LMAPublishError(f"Unknown package type: {stack_def.package_type}")
        return handler(
            stack_def=stack_def,
            project_dir=project_dir,
            bucket=bucket,
            bucket_basename=bucket_basename,
            prefix_and_version=prefix_and_version,
            region=region,
            version=version,
            tmpdir=tmpdir,
        )

    def _publish_zip_and_upload(self, *, stack_def, project_dir, bucket, prefix_and_version, region, tmpdir, **_kw) -> dict:
        """Zip source and upload (for lma-virtual-participant-stack)."""
        stack_dir = project_dir / stack_def.name
        content_hash = _calculate_dir_hash(stack_dir)
        zip_filename = f"src-{content_hash}.zip"
        zip_path = tmpdir / zip_filename

        # Create zip excluding node_modules, build, dist
        skip_dirs = {"node_modules", "build", "dist", ".git", "__pycache__"}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(stack_dir):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for fname in files:
                    fpath = Path(root) / fname
                    arcname = fpath.relative_to(stack_dir)
                    zf.write(fpath, arcname)

        # Upload zip
        s3_zip_key = f"{prefix_and_version}/{stack_def.name}/{zip_filename}"
        vp_src_s3_location = f"{bucket}/{s3_zip_key}"
        logger.info("Uploading %s to s3://%s", zip_filename, vp_src_s3_location)
        self._s3.upload_file(str(zip_path), bucket, s3_zip_key)

        # Upload template
        template_path = stack_dir / stack_def.template_file
        s3_template_key = f"{prefix_and_version}/{stack_def.name}/template.yaml"
        self._s3.upload_file(str(template_path), bucket, s3_template_key)

        # Validate template
        https_url = f"https://s3.{region}.amazonaws.com/{bucket}/{s3_template_key}"
        self._validate_template(https_url)

        return {
            "vp_src_s3_location": vp_src_s3_location,
            "s3_template_url": https_url,
            "message": f"Zipped and uploaded ({zip_filename})",
        }

    def _publish_zip_with_token_replace(
        self, *, stack_def, project_dir, bucket, prefix_and_version, region, version, tmpdir, **_kw
    ) -> dict:
        """Zip source with VERSION token replacement, upload zip and template (for browser extension)."""
        stack_dir = project_dir / stack_def.name
        content_hash = _calculate_dir_hash(stack_dir)
        zip_filename = f"src-{content_hash}.zip"

        # Copy to temp dir for token replacement
        temp_stack_dir = tmpdir / f"{stack_def.name}-temp"
        skip_dirs = {"node_modules", "build", "dist", ".git", "__pycache__"}
        shutil.copytree(stack_dir, temp_stack_dir, ignore=shutil.ignore_patterns(*skip_dirs))

        # Replace <VERSION_TOKEN> in key files
        token_files = ["package.json", "public/manifest.json", "template.yaml"]
        for rel_path in token_files:
            fpath = temp_stack_dir / rel_path
            if fpath.exists():
                content = fpath.read_text()
                fpath.write_text(content.replace("<VERSION_TOKEN>", version))

        # Create zip from token-replaced copy
        zip_path = tmpdir / zip_filename
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(temp_stack_dir):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for fname in files:
                    fpath = Path(root) / fname
                    arcname = fpath.relative_to(temp_stack_dir)
                    zf.write(fpath, arcname)

        # Upload zip
        s3_zip_key = f"{prefix_and_version}/{stack_def.name}/{zip_filename}"
        src_s3_location = f"{bucket}/{s3_zip_key}"
        logger.info("Uploading %s to s3://%s", zip_filename, src_s3_location)
        self._s3.upload_file(str(zip_path), bucket, s3_zip_key)

        # Upload token-replaced template
        template_path = temp_stack_dir / stack_def.template_file
        s3_template_key = f"{prefix_and_version}/{stack_def.name}/template.yaml"
        self._s3.upload_file(str(template_path), bucket, s3_template_key)

        # Validate template
        https_url = f"https://s3.{region}.amazonaws.com/{bucket}/{s3_template_key}"
        self._validate_template(https_url)

        # Cleanup temp copy
        shutil.rmtree(temp_stack_dir, ignore_errors=True)

        return {
            "browser_ext_src_s3_location": src_s3_location,
            "s3_template_url": https_url,
            "message": f"Zipped with token replacement and uploaded ({zip_filename})",
        }

    def _publish_cfn_package(self, *, stack_def, project_dir, bucket, prefix_and_version, region, tmpdir, **_kw) -> dict:
        """Package with `aws cloudformation package`."""
        if stack_def.deployment_subdir:
            work_dir = project_dir / stack_def.name / stack_def.deployment_subdir
        else:
            work_dir = project_dir / stack_def.name

        template_file = stack_def.template_file
        output_file = tmpdir / template_file
        s3_prefix = f"{prefix_and_version}/{stack_def.name}"

        cmd = [
            "aws", "cloudformation", "package",
            "--template-file", template_file,
            "--output-template-file", str(output_file),
            "--s3-bucket", bucket,
            "--s3-prefix", s3_prefix,
            "--region", region,
        ]
        self._run_command(cmd, cwd=str(work_dir), desc=f"cfn package {stack_def.name}")

        # Upload packaged template
        s3_template_key = f"{prefix_and_version}/{stack_def.name}/template.yaml"
        self._s3.upload_file(str(output_file), bucket, s3_template_key)

        # Validate
        https_url = f"https://s3.{region}.amazonaws.com/{bucket}/{s3_template_key}"
        self._validate_template(https_url)

        return {"s3_template_url": https_url, "message": "Packaged and validated"}

    def _publish_delegate_script(self, *, stack_def, project_dir, bucket, prefix_and_version, region, tmpdir, **_kw) -> dict:
        """Delegate to stack's own publish.sh."""
        stack_dir = project_dir / stack_def.name
        script = stack_dir / stack_def.delegate_script

        if not script.exists():
            raise LMAPublishError(f"Delegate script not found: {script}")

        script.chmod(0o755)
        cmd = [str(script), bucket, prefix_and_version, region]
        self._run_command(cmd, cwd=str(stack_dir), desc=f"delegate {stack_def.name}")

        return {"message": f"Delegated to {stack_def.delegate_script}"}

    def _publish_build_script(
        self, *, stack_def, project_dir, bucket, bucket_basename, prefix_and_version, region, version, tmpdir, **_kw
    ) -> dict:
        """Run build-s3-dist.sh (for lma-ai-stack, lma-websocket-transcriber-stack)."""
        stack_dir = project_dir / stack_def.name
        deployment_dir = stack_dir / (stack_def.deployment_subdir or "deployment")
        script = deployment_dir / stack_def.build_script

        if not script.exists():
            raise LMAPublishError(f"Build script not found: {script}")

        # Clean previous output
        out_dir = stack_dir / "out"
        if out_dir.exists():
            shutil.rmtree(out_dir)

        script.chmod(0o755)
        s3_prefix = f"{prefix_and_version}/{stack_def.name}"
        cmd = [str(script), bucket_basename, s3_prefix, version, region]
        self._run_command(cmd, cwd=str(deployment_dir), desc=f"build {stack_def.name}")

        return {"message": f"Built via {stack_def.build_script}"}

    def _publish_hash_and_package(
        self, *, stack_def, project_dir, bucket, prefix_and_version, region, tmpdir, **_kw
    ) -> dict:
        """Compute source hash, inject into template, then cfn package."""
        stack_dir = project_dir / stack_def.name
        deployment_dir = stack_dir / (stack_def.deployment_subdir or "deployment")
        source_dir = (deployment_dir / stack_def.hash_source_dir).resolve() if stack_def.hash_source_dir else stack_dir

        # Compute hash of source
        content_hash = _calculate_dir_hash(source_dir)
        logger.info("Source hash for %s: %s", stack_def.name, content_hash)

        # Read and patch template
        template_path = deployment_dir / stack_def.template_file
        template_content = template_path.read_text()
        pattern = rf"({stack_def.hash_template_field}: ).*"
        patched = re.sub(pattern, rf"\g<1>{content_hash}", template_content)
        template_path.write_text(patched)

        # cfn package
        output_file = tmpdir / stack_def.template_file
        s3_prefix = f"{prefix_and_version}/{stack_def.name}"
        cmd = [
            "aws", "cloudformation", "package",
            "--template-file", stack_def.template_file,
            "--output-template-file", str(output_file),
            "--s3-bucket", bucket,
            "--s3-prefix", s3_prefix,
            "--region", region,
        ]
        self._run_command(cmd, cwd=str(deployment_dir), desc=f"cfn package {stack_def.name}")

        # Upload
        s3_path = stack_def.s3_template_path or f"{stack_def.template_file}"
        s3_template_key = f"{prefix_and_version}/{stack_def.name}/{s3_path}"
        self._s3.upload_file(str(output_file), bucket, s3_template_key)

        return {
            "s3_template_url": f"https://s3.{region}.amazonaws.com/{bucket}/{s3_template_key}",
            "message": f"Hashed ({content_hash}), packaged and uploaded",
        }

    # ── Main template ─────────────────────────────────────────

    def _publish_main_template(
        self,
        project_dir: Path,
        bucket: str,
        prefix: str,
        prefix_and_version: str,
        region: str,
        version: str,
        vp_src_s3_location: str,
        browser_ext_src_s3_location: str = "",
        tmpdir: Path = None,
    ) -> dict[str, Any]:
        """Replace tokens in lma-main.yaml, upload, and validate."""
        main_template_path = project_dir / "lma-main.yaml"
        content = main_template_path.read_text()

        replacements = {
            "<ARTIFACT_BUCKET_TOKEN>": bucket,
            "<ARTIFACT_PREFIX_TOKEN>": prefix_and_version,
            "<VERSION_TOKEN>": version,
            "<REGION_TOKEN>": region,
            "<BROWSER_EXTENSION_SRC_S3_LOCATION_TOKEN>": browser_ext_src_s3_location,
            "<VIRTUAL_PARTICIPANT_SRC_S3_LOCATION_TOKEN>": vp_src_s3_location,
        }
        for token, value in replacements.items():
            content = content.replace(token, value)

        # Write to temp
        output_path = tmpdir / "lma-main.yaml"
        output_path.write_text(content)

        # Upload
        s3_key = f"{prefix}/lma-main.yaml"
        self._s3.upload_file(str(output_path), bucket, s3_key)

        # Validate
        template_url = f"https://s3.{region}.amazonaws.com/{bucket}/{s3_key}"
        try:
            self._validate_template(template_url)
            return {"success": True, "template_url": template_url}
        except LMAPublishError as e:
            return {"success": False, "template_url": template_url, "error": str(e)}

    # ── Utilities ─────────────────────────────────────────────

    def _validate_template(self, template_url: str) -> None:
        """Validate a CloudFormation template URL."""
        try:
            self._cfn.validate_template(TemplateURL=template_url)
        except botocore.exceptions.ClientError as e:
            raise LMAPublishError(f"Template validation failed: {e}") from e

    def _set_public_acls(self, bucket: str, prefix_and_version: str, prefix: str) -> None:
        """Set public-read ACLs on all published artifacts."""
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix_and_version):
            for obj in page.get("Contents", []):
                self._s3.put_object_acl(ACL="public-read", Bucket=bucket, Key=obj["Key"])

        # Also the main template
        self._s3.put_object_acl(
            ACL="public-read", Bucket=bucket, Key=f"{prefix}/lma-main.yaml"
        )

    @staticmethod
    def _run_command(
        cmd: list[str],
        cwd: str | None = None,
        desc: str = "",
        timeout: int = 1200,
    ) -> subprocess.CompletedProcess:
        """Run a shell command, raising LMAPublishError on failure.

        Automatically sets DOCKER_DEFAULT_PLATFORM=linux/amd64 on Apple Silicon
        Macs to ensure Lambda-compatible container builds.
        """
        import platform as _platform

        logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)

        # On Apple Silicon (arm64) Macs, Lambda containers need linux/amd64
        env = os.environ.copy()
        if _platform.system() == "Darwin" and _platform.machine() == "arm64":
            env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
            # Also set SAM CLI to use container for correct architecture
            env.setdefault("SAM_CLI_CONTAINER_CONNECTION_TIMEOUT", "60")

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                raise LMAPublishError(
                    f"Command failed ({desc}): {stderr}"
                )
            return result
        except subprocess.TimeoutExpired:
            raise LMAPublishError(f"Command timed out ({desc}): {' '.join(cmd)}")
        except FileNotFoundError:
            raise LMAPublishError(f"Command not found ({desc}): {cmd[0]}")
