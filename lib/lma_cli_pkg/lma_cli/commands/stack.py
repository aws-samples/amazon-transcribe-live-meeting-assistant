# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Stack commands — status, outputs, deploy, delete, logs."""

from __future__ import annotations

import os
import sys
from typing import Optional

import click

from lma_cli.formatters import (
    console,
    print_error,
    print_info,
    print_key_value,
    print_success,
    print_warning,
    stack_info_table,
    stack_outputs_table,
)

# Region-specific public template URLs
TEMPLATE_URLS = {
    "us-east-1": "https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/lma/lma-main.yaml",
    "us-west-2": "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/lma/lma-main.yaml",
    "ap-southeast-2": (
        "https://s3.ap-southeast-2.amazonaws.com/"
        "aws-bigdata-blog-replica-ap-southeast-2/artifacts/lma/lma-main.yaml"
    ),
}


def _build_from_local_code(
    source_dir: str,
    region: str,
    client_factory,
    bucket_basename: Optional[str] = None,
    prefix: Optional[str] = None,
    public: bool = False,
    clean_build: bool = False,
    no_validate: bool = False,
):
    """Build and publish LMA artifacts from local code, returning the template URL.

    Returns:
        Tuple of (template_url, template_file) — one will be set.
    """
    from lma_sdk import LMAClient
    from lma_sdk.exceptions import LMAError

    client: LMAClient = client_factory(region=region)

    # Auto-generate bucket basename if not provided
    if not bucket_basename:
        try:
            sts = client.session.client("sts")
            account_id = sts.get_caller_identity()["Account"]
            bucket_basename = f"lma-{account_id}"
            print_info(f"Auto-generated bucket basename: {bucket_basename}")
        except Exception:
            raise click.ClickException(
                "--bucket-basename is required (could not auto-detect AWS account ID)."
            )

    resolved_prefix = prefix or "lma"

    console.print()
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print("[bold cyan] 📦 BUILDING FROM LOCAL CODE[/bold cyan]")
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print(f"[cyan]▶ Region:[/cyan]  {region}")
    console.print(f"[cyan]▶ Bucket:[/cyan]  {bucket_basename}-{region}")
    console.print(f"[cyan]▶ Prefix:[/cyan]  {resolved_prefix}")
    console.print(f"[cyan]▶ Source:[/cyan]  {os.path.abspath(source_dir)}")
    console.print()

    def progress_callback(stack_name: str, message: str) -> None:
        """Display step-by-step progress."""
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
            prefix=resolved_prefix,
            region=region,
            public=public,
            project_dir=source_dir,
            force=clean_build,
            progress_callback=progress_callback,
        )
    except LMAError as e:
        raise click.ClickException(f"Build failed: {e}")

    if not result.success:
        failed = [sr for sr in result.stack_results if not sr.success and not sr.skipped]
        if failed:
            for sr in failed:
                console.print(f"[red]  ✗ {sr.stack_name}: {sr.message}[/red]")
        raise click.ClickException("Build from local code failed.")

    console.print("[green]✓ Build complete![/green]")
    console.print()

    # Return template URL from publish result
    template_url = result.template_url if hasattr(result, "template_url") else None
    if template_url:
        return template_url, None

    # Fall back to constructing URL from known pattern
    bucket_name = f"{bucket_basename}-{region}"
    template_url = f"https://s3.{region}.amazonaws.com/{bucket_name}/{resolved_prefix}/lma-main.yaml"
    return template_url, None


def _format_event_line(event) -> str:
    """Format a stack event for console display."""
    ts = event.timestamp.strftime("%H:%M:%S")
    status = event.resource_status
    resource = event.logical_resource_id
    reason = event.resource_status_reason

    # Color based on status
    if "COMPLETE" in status and "ROLLBACK" not in status:
        status_color = "green"
    elif "FAILED" in status or "ROLLBACK" in status:
        status_color = "red"
    elif "IN_PROGRESS" in status:
        status_color = "yellow"
    else:
        status_color = "dim"

    line = f"[dim]{ts}[/dim] [{status_color}]{status:<40}[/{status_color}] [cyan]{resource}[/cyan]"
    if reason:
        line += f" [dim]({reason})[/dim]"
    return line


def _display_deployment_success(stack_name: str, result, outputs=None):
    """Display success message and next steps after deployment."""
    operation = getattr(result, "operation", "deploy")
    console.print(
        f"\n[green]✓ Stack {operation.lower()} completed successfully![/green]\n"
    )

    # Show important outputs
    if outputs:
        console.print("[bold]Important Outputs:[/bold]")
        for key in ["CloudFrontEndpoint", "ApplicationWebURL", "WebAppCloudFrontUrl"]:
            if key in outputs:
                console.print(f"  Application URL: [cyan]{outputs[key]}[/cyan]")
                break
        for key in ["WebSocketEndpoint", "WebSocketURI"]:
            if key in outputs:
                console.print(f"  WebSocket URL: {outputs[key]}")
                break
        console.print()

    console.print("[bold]Next Steps:[/bold]")
    console.print("1. Check your email for temporary admin password")
    console.print("2. Enable Bedrock model access (see README)")
    console.print("3. Open the application URL above to get started")
    console.print(
        f"   [cyan]lma-cli outputs --stack-name {stack_name}[/cyan] — to see all outputs"
    )
    console.print()


def _display_deployment_failure(stack_name: str, result, console_url=None):
    """Display failure message after deployment."""
    operation = getattr(result, "operation", "deploy")
    status = getattr(result, "status", "FAILED")
    error = getattr(result, "error", None) or getattr(result, "message", "Unknown error")

    console.print(f"\n[red]✗ Stack {operation.lower()} failed[/red]")
    console.print(f"  Status: [red]{status}[/red]")
    console.print(f"  Error: {error}")
    if console_url:
        console.print(f"  Console: [link={console_url}]{console_url}[/link]")
    console.print()
    console.print("[bold]Troubleshooting:[/bold]")
    console.print(f"  [cyan]lma-cli logs --stack-name {stack_name} --list[/cyan] — view logs")
    console.print("  Check CloudFormation console for detailed event history")
    console.print()


@click.command("status")
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.pass_context
def status_cmd(ctx, stack_name, region):
    """Show the current status of the LMA CloudFormation stack.

    \b
    Examples:
      lma status
      lma status --stack-name MyLMA
    """
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)

    try:
        result = client.stack.status()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if not result.exists:
        print_warning(result.message or "Stack not found.")
        return

    console.print(stack_info_table(result.stack))

    if result.stack.outputs:
        console.print()
        console.print(stack_outputs_table(result.stack.outputs))


@click.command("outputs")
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def outputs_cmd(ctx, stack_name, region, as_json):
    """Show CloudFormation stack outputs (URLs, ARNs, resource IDs).

    \b
    Examples:
      lma outputs
      lma outputs --json
      lma outputs --stack-name MyLMA
    """
    import json

    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)

    try:
        outputs = client.stack.outputs()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        data = {k: {"value": v.value, "description": v.description} for k, v in outputs.items()}
        console.print_json(json.dumps(data, indent=2))
    else:
        output_list = list(outputs.values())
        if output_list:
            console.print(stack_outputs_table(output_list))
        else:
            print_warning("Stack has no outputs.")


@click.command("deploy")
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option(
    "--admin-email",
    default=None,
    help="Admin user email address (required for new stacks).",
)
@click.option(
    "--from-code",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    default=None,
    help="Deploy from local code by building and publishing first (path to project root).",
)
@click.option(
    "--template-url",
    default=None,
    help="S3 URL for the CloudFormation template (default: auto-selected based on region).",
)
@click.option(
    "--template-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a local CloudFormation template file.",
)
@click.option(
    "--parameter", "-p",
    multiple=True,
    help="Parameter override (Key=Value). Can be repeated.",
)
@click.option("--wait", is_flag=True, default=False, help="Wait for stack operation to complete.")
@click.option(
    "--no-rollback",
    is_flag=True,
    default=False,
    help="Disable rollback on stack creation failure.",
)
@click.option("--role-arn", default=None, help="CloudFormation service role ARN.")
@click.option(
    "--timeout",
    default=120,
    type=int,
    help="Max wait time in minutes.",
    show_default=True,
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket basename for artifacts (used with --from-code).",
)
@click.option(
    "--prefix",
    default=None,
    help="S3 key prefix for artifacts (default: lma, used with --from-code).",
)
@click.option(
    "--public",
    is_flag=True,
    default=False,
    help="Make S3 artifacts publicly readable (used with --from-code).",
)
@click.option(
    "--clean-build",
    is_flag=True,
    default=False,
    help="Force full rebuild by deleting checksums (used with --from-code).",
)
@click.option(
    "--no-validate-template",
    is_flag=True,
    default=False,
    help="Skip CloudFormation template validation (used with --from-code).",
)
@click.pass_context
def deploy_cmd(
    ctx,
    stack_name,
    region,
    admin_email,
    from_code,
    template_url,
    template_file,
    parameter,
    wait,
    no_rollback,
    role_arn,
    timeout,
    bucket_basename,
    prefix,
    public,
    clean_build,
    no_validate_template,
):
    """Deploy or update the LMA CloudFormation stack.

    \b
    If no template source is specified, deploys from the public published
    LMA template for the current region. Use --from-code to build and
    publish from local source code before deploying.

    \b
    For new stacks, --admin-email is required.
    For existing stacks, only specify parameters you want to update.

    \b
    Examples:
      # Deploy from public template (simplest — new stack)
      lma deploy --stack-name MyLMA --admin-email user@example.com --wait

      # Deploy from public template (update existing)
      lma deploy --stack-name MyLMA --wait

      # Deploy from local code
      lma deploy --stack-name MyLMA --from-code . --admin-email user@example.com --wait

      # Deploy from local code with custom bucket
      lma deploy --stack-name MyLMA --from-code . --bucket-basename my-artifacts --wait

      # Deploy from specific template URL
      lma deploy --stack-name MyLMA --template-url https://s3.us-east-1.amazonaws.com/bucket/lma-main.yaml

      # Deploy with parameter overrides
      lma deploy --stack-name MyLMA -p AdminEmail=admin@example.com -p InstallDemoMode=true --wait
    """
    from lma_sdk.exceptions import LMAError

    try:
        # Validate mutually exclusive template options
        exclusive_count = sum(1 for x in [from_code, template_url, template_file] if x)
        if exclusive_count > 1:
            print_error("Cannot specify more than one of --from-code, --template-url, --template-file")
            sys.exit(1)

        # Auto-detect region if not provided
        if not region:
            import boto3

            session = boto3.session.Session()
            region = session.region_name
            if not region:
                print_error(
                    "Region could not be determined. "
                    "Please specify --region or configure AWS_DEFAULT_REGION."
                )
                sys.exit(1)

        # Handle deployment from local code
        template_path = None
        if from_code:
            template_url, template_path = _build_from_local_code(
                source_dir=from_code,
                region=region,
                client_factory=ctx.obj["client_factory"],
                bucket_basename=bucket_basename,
                prefix=prefix,
                public=public,
                clean_build=clean_build,
                no_validate=no_validate_template,
            )

        # Handle local template file
        elif template_file:
            template_path = os.path.abspath(template_file)
            console.print(f"[bold]Using local template: {template_path}[/bold]")

        # Default: use public template URL for the region
        elif not template_url:
            if region in TEMPLATE_URLS:
                template_url = TEMPLATE_URLS[region]
                console.print(f"[bold]Using public template for region: {region}[/bold]")
            else:
                supported_regions = ", ".join(TEMPLATE_URLS.keys())
                print_error(
                    f"Region '{region}' does not have a published template. "
                    f"Supported regions: {supported_regions}. "
                    f"Please provide --template-url or --from-code explicitly."
                )
                sys.exit(1)

        # Initialize client
        client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
        resolved_stack_name = client.stack_name or stack_name or "LMA"

        # Check if stack has an operation in progress
        in_progress = client.stack.check_in_progress()
        if in_progress:
            operation = in_progress.operation
            status = in_progress.status

            console.print(
                f"[bold yellow]Stack '{resolved_stack_name}' has an operation in progress[/bold yellow]"
            )
            console.print(f"Current status: [cyan]{status}[/cyan]")
            console.print()

            if not wait:
                console.print("[bold]Use --wait to monitor the operation:[/bold]")
                console.print(
                    f"  [cyan]lma-cli deploy --stack-name {resolved_stack_name} --wait[/cyan]"
                )
                return

            console.print("[bold]Switching to monitoring mode...[/bold]")
            console.print()

            # Monitor the existing operation with event streaming
            def event_cb(event):
                console.print(_format_event_line(event))

            result = client.stack.monitor(
                operation=operation,
                event_callback=event_cb,
            )

            if result.success:
                _display_deployment_success(
                    resolved_stack_name, result, outputs=result.outputs
                )
            else:
                _display_deployment_failure(resolved_stack_name, result)
                sys.exit(1)
            return

        # Check if stack exists (for admin-email validation)
        stack_exists = client.stack.exists()

        if stack_exists:
            console.print(
                f"[bold blue]Updating existing LMA stack: {resolved_stack_name}[/bold blue]"
            )
            if admin_email:
                console.print(f"Admin Email: {admin_email}")
        else:
            console.print(
                f"[bold blue]Creating new LMA stack: {resolved_stack_name}[/bold blue]"
            )
            if not admin_email:
                print_error("--admin-email is required when creating a new stack")
                sys.exit(1)
            console.print(f"Admin Email: {admin_email}")

        console.print()

        # Parse parameter overrides
        params = {}
        if admin_email:
            params["AdminEmail"] = admin_email
        for p in parameter:
            if "=" not in p:
                print_error(f"Invalid parameter format: '{p}'. Use Key=Value.")
                sys.exit(1)
            key, value = p.split("=", 1)
            params[key] = value

        # Deploy stack (without waiting — we'll monitor separately for event streaming)
        if wait:
            # Deploy without internal wait, then use monitor for event streaming
            with console.status("[bold green]Initiating deployment..."):
                result = client.stack.deploy(
                    template_url=template_url,
                    template_file=template_path,
                    parameters=params or None,
                    wait=False,
                    timeout_minutes=timeout,
                    no_rollback=no_rollback,
                    role_arn=role_arn,
                )

            # Check if deploy was a no-op
            if result.success and result.status in ("UPDATE_COMPLETE",):
                print_success(result.message)
                return

            # Determine the operation type
            operation = result.operation or ("CREATE" if not stack_exists else "UPDATE")
            console.print(
                f"[bold green]✓ Stack {operation.lower()} initiated[/bold green]"
            )
            console.print()
            console.print("[bold]Monitoring stack events...[/bold]")
            console.print()

            # Monitor with event streaming
            def event_cb(event):
                console.print(_format_event_line(event))

            monitor_result = client.stack.monitor(
                operation=operation,
                event_callback=event_cb,
                timeout_seconds=timeout * 60,
            )

            if monitor_result.success:
                _display_deployment_success(
                    resolved_stack_name,
                    monitor_result,
                    outputs=monitor_result.outputs,
                )
            else:
                _display_deployment_failure(
                    resolved_stack_name,
                    monitor_result,
                    console_url=None,
                )
                sys.exit(1)
        else:
            # Deploy without waiting
            with console.status("[bold green]Deploying stack..."):
                result = client.stack.deploy(
                    template_url=template_url,
                    template_file=template_path,
                    parameters=params or None,
                    wait=False,
                    timeout_minutes=timeout,
                    no_rollback=no_rollback,
                    role_arn=role_arn,
                )

            is_success = result.success or result.status.endswith("_IN_PROGRESS")
            if is_success:
                operation = result.operation or "deploy"
                console.print(
                    f"\n[green]✓ Stack {operation.lower()} initiated successfully![/green]\n"
                )
                console.print("[bold]Monitor progress:[/bold]")
                console.print(
                    f"  AWS Console: CloudFormation → Stacks → {resolved_stack_name}"
                )
                console.print()
                console.print("[bold]Or use --wait flag to monitor in CLI:[/bold]")
                console.print(
                    f"  [cyan]lma-cli deploy --stack-name {resolved_stack_name} --wait[/cyan]"
                )
                console.print()
            else:
                print_error(f"Deploy failed: {result.message}")
                if result.console_url:
                    print_key_value("Console URL", result.console_url)
                sys.exit(1)

    except click.ClickException:
        raise
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)
    except Exception as e:
        import logging

        logging.getLogger(__name__).error("Error deploying stack: %s", e, exc_info=True)
        print_error(str(e))
        sys.exit(1)


@click.command("delete")
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--no-wait", is_flag=True, default=False, help="Don't wait for deletion.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def delete_cmd(ctx, stack_name, region, no_wait, yes):
    """Delete the LMA CloudFormation stack.

    ⚠️  This permanently deletes all stack resources. Use with caution.

    \b
    Examples:
      lma delete
      lma delete --stack-name MyLMA --yes
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    name = client.stack_name

    if not yes:
        click.confirm(
            f"⚠️  Delete stack '{name}' and ALL its resources? This cannot be undone.",
            abort=True,
        )

    try:
        if not no_wait:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                progress.add_task(f"[heading]Deleting stack '{name}'...", total=None)
                result = client.stack.delete(wait=True)
        else:
            result = client.stack.delete(wait=False)
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if result.success:
        print_success(result.message)
    else:
        print_error(result.message)
        sys.exit(1)


@click.command("logs")
@click.argument("log_group", required=False, default=None)
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--since", default=15, type=int, help="Show logs from last N minutes.", show_default=True)
@click.option("--limit", default=100, type=int, help="Max log entries to show.", show_default=True)
@click.option("--list", "list_groups", is_flag=True, default=False, help="List available log groups.")
@click.pass_context
def logs_cmd(ctx, log_group, stack_name, region, since, limit, list_groups):
    """View CloudWatch logs for LMA Lambda functions and services.

    Without arguments, lists available log groups. Provide a LOG_GROUP name
    (or substring) to view recent log entries.

    \b
    Examples:
      lma logs --list
      lma logs /LMA/lambda/FetchTranscript
      lma logs FetchTranscript --since 60
    """
    from rich.table import Table

    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)

    try:
        all_groups = client.stack.get_log_groups()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if list_groups or log_group is None:
        if not all_groups:
            print_warning("No log groups found for this stack.")
            return

        table = Table(title="Log Groups", expand=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Log Group", style="key")

        for i, lg in enumerate(sorted(all_groups), 1):
            table.add_row(str(i), lg)

        console.print(table)
        print_info("Use 'lma logs <log-group-name>' to view entries.")
        return

    # Resolve partial log group name
    matches = [g for g in all_groups if log_group in g]
    if not matches:
        print_error(f"No log group matching '{log_group}' found.")
        print_info("Use 'lma logs --list' to see available groups.")
        sys.exit(1)
    if len(matches) > 1:
        print_warning(f"Multiple log groups match '{log_group}':")
        for m in matches:
            console.print(f"  • {m}")
        print_info("Please be more specific.")
        sys.exit(1)

    resolved_group = matches[0]
    print_info(f"Showing last {since} min of logs from: {resolved_group}")

    try:
        entries = client.stack.tail_logs(resolved_group, since_minutes=since, limit=limit)
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if not entries:
        print_warning("No log entries found in the specified time range.")
        return

    for entry in entries:
        ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        console.print(f"[dim]{ts}[/dim] {entry.message}")
