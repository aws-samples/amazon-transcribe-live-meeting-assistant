# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Publish command — build and upload LMA artifacts to S3."""

from __future__ import annotations

import sys

import click
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from lma_cli.formatters import (
    console,
    print_error,
    print_info,
    print_success,
    print_warning,
    publish_result_panel,
)


@click.command("publish")
@click.option("--source-dir", default=".", type=click.Path(exists=True), help="Path to LMA project root directory.", show_default=True)
@click.option("--bucket-basename", default=None, help="S3 bucket basename for artifacts — region is appended automatically (auto-generated if not provided).")
@click.option("--prefix", default="lma", help="S3 key prefix for artifacts.", show_default=True)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region for deployment (default: from AWS CLI profile).")
@click.option("--public", is_flag=True, default=False, help="Make S3 artifacts publicly readable.")
@click.option("--force", "force", is_flag=True, default=False, help="Delete all checksum files to force full rebuild.")
@click.option(
    "--allow-untracked",
    is_flag=True,
    default=False,
    help=(
        "Bypass the safety check that blocks publishing when BUILD_SCRIPT stacks "
        "contain untracked files. Untracked files are silently excluded from the "
        "`git ls-files`-based source bundle, so this is NOT recommended."
    ),
)
@click.option("--version", "version_override", default="", help="Override version (default: read from VERSION file).")
@click.option("--no-validate", is_flag=True, default=False, help="Skip CloudFormation template validation.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose build output.")
@click.pass_context
def publish_cmd(
    ctx,
    source_dir,
    bucket_basename,
    prefix,
    region,
    public,
    force,
    allow_untracked,
    version_override,
    no_validate,
    verbose,
):
    """Build, package, and publish LMA CloudFormation artifacts to S3.

    This is the Python replacement for publish.sh. It packages all LMA sub-stacks,
    uploads artifacts to S3, and generates a deployable CloudFormation template URL.

    \b
    Examples:
      # Standard build and publish
      lma publish --source-dir . --region us-east-1

      # With custom bucket and prefix
      lma publish --source-dir . --bucket-basename my-artifacts --prefix lma --region us-east-1

      # Force full rebuild (skip change detection)
      lma publish --source-dir . --region us-east-1 --force

      # Public artifacts (for shared deployments)
      lma publish --source-dir . --region us-east-1 --public
    """
    import os
    from lma_sdk import LMAClient
    from lma_sdk.exceptions import LMAError

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

    client: LMAClient = ctx.obj["client_factory"](region=region)

    # Auto-generate bucket basename if not provided (same pattern as IDP)
    if not bucket_basename:
        try:
            sts = client.session.client("sts")
            account_id = sts.get_caller_identity()["Account"]
            bucket_basename = f"lma-{account_id}"
            print_info(f"Auto-generated bucket basename: {bucket_basename}")
        except Exception:
            print_error("--bucket-basename is required (could not auto-detect AWS account ID).")
            sys.exit(1)

    # Resolve region from client (which gets it from profile if not explicit)
    resolved_region = region or client.region
    if not resolved_region:
        print_error("Could not determine AWS region. Use --region or configure your AWS CLI profile.")
        sys.exit(1)

    # Print publish header
    console.print()
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print("[bold cyan] 📦 PUBLISH LMA ARTIFACTS[/bold cyan]")
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print(f"[cyan]▶ Region:[/cyan]  {resolved_region}")
    console.print(f"[cyan]▶ Bucket:[/cyan]  {bucket_basename}-{resolved_region}")
    console.print(f"[cyan]▶ Prefix:[/cyan]  {prefix}")
    console.print(f"[cyan]▶ Source:[/cyan]  {os.path.abspath(source_dir)}")
    console.print()

    def progress_callback(stack_name: str, message: str) -> None:
        """Display step-by-step progress (IDP-style)."""
        if "Checking" in message or "Ensuring" in message:
            console.print(f"[cyan]▶ {message}[/cyan]")
        elif "Skipped" in message or "unchanged" in message.lower():
            console.print(f"[dim]  ⊘ {stack_name} — {message}[/dim]")
        elif "Packaging" in message:
            console.print(f"[cyan]▶ {stack_name}[/cyan]")
        elif "Done" in message:
            console.print(f"[green]  ✓ {stack_name} — {message}[/green]")
        elif "FAILED" in message:
            console.print(f"[red]  ✗ {stack_name} — {message}[/red]")
        elif "main" in stack_name.lower():
            console.print(f"[cyan]▶ {message}[/cyan]")
        else:
            console.print(f"[dim]  └─ {stack_name}: {message}[/dim]")

    try:
        result = client.publish.publish(
            bucket_basename=bucket_basename,
            prefix=prefix,
            region=resolved_region,
            public=public,
            project_dir=source_dir,
            version=version_override,
            force=force,
            allow_untracked=allow_untracked,
            progress_callback=progress_callback,
        )
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    # Display results
    console.print()
    publish_result_panel(result)

    if result.success:
        console.print("[bold green]✅ Publish complete![/bold green]")
    else:
        # Show clear failure summary with actionable guidance
        failed = [sr for sr in result.stack_results if not sr.success and not sr.skipped]
        if failed:
            console.print(f"[bold red]❌ Publish failed — {len(failed)} stack(s) had errors:[/bold red]")
            for sr in failed:
                console.print(f"[red]  • {sr.stack_name}[/red]")
                # Detect Docker ARM issues and provide guidance
                if "exec format error" in sr.message or "platform" in sr.message.lower():
                    console.print()
                    console.print("[yellow]  💡 Docker ARM/x86 compatibility issue detected.[/yellow]")
                    console.print("[yellow]     Fix: Open Docker Desktop → Settings → General →[/yellow]")
                    console.print("[yellow]     Enable 'Use Rosetta for x86_64/amd64 emulation on Apple Silicon'[/yellow]")
                    console.print("[yellow]     Then restart Docker Desktop and retry.[/yellow]")
        console.print()
        sys.exit(1)


@click.command("check-prereqs")
def check_prereqs_cmd():
    """Check if all publish prerequisites are installed.

    Verifies Docker, SAM CLI, Node.js, npm, pip3, zip, and virtualenv
    are available and meet minimum version requirements.
    """
    from lma_sdk._core.publish import check_prerequisites

    print_info("Checking publish prerequisites...")
    errors = check_prerequisites()

    if errors:
        for err in errors:
            print_error(err)
        print_warning(f"{len(errors)} prerequisite(s) not met.")
        sys.exit(1)
    else:
        print_success("All prerequisites met!")


@click.command("list-stacks")
def list_stacks_cmd():
    """List all publishable LMA sub-stacks."""
    from lma_sdk._core.publish import STACK_DEFINITIONS

    from rich.table import Table

    table = Table(title="LMA Sub-Stacks", expand=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Stack Name", style="key")
    table.add_column("Package Type", style="value")
    table.add_column("Change Detection", style="value")

    for i, sd in enumerate(STACK_DEFINITIONS, 1):
        cd = "✓" if sd.supports_change_detection else "—"
        table.add_row(str(i), sd.name, sd.package_type.value, cd)

    console.print(table)
