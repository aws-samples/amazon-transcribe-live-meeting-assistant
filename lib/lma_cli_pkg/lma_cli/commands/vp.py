# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Virtual Participant commands — create, get, end, list.

Usage::

    lma vp create --name "Weekly sync" --platform ZOOM --id 1234567890
    lma vp get --id <vp-id>
    lma vp end --id <vp-id>
    lma vp list
"""

from __future__ import annotations

import json
import sys

import click

from lma_cli.formatters import console, print_error, print_info, print_success


PLATFORM_CHOICES = click.Choice(
    ["ZOOM", "TEAMS", "CHIME", "WEBEX"],
    case_sensitive=False,
)


@click.group("vp")
def vp_cmd() -> None:
    """Virtual Participant operations (create, get, end, list)."""


@vp_cmd.command("create")
@click.option(
    "--stack-name",
    default=None,
    envvar="LMA_STACK_NAME",
    help="CloudFormation stack name.",
)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--name", "meeting_name", required=True, help="Meeting name.")
@click.option(
    "--platform", "platform", required=True, type=PLATFORM_CHOICES,
    help="Meeting platform.",
)
@click.option("--id", "meeting_id", required=True, help="Meeting ID.")
@click.option("--password", default="", help="Meeting password (if any).")
@click.option(
    "--user-name", default="lma-cli@lma",
    help="Display name the scribe reports (e.g. Zoom participant name).",
)
@click.option(
    "--wait/--no-wait", default=True,
    help="Poll until VP leaves INITIALIZING (default: wait).",
)
@click.option(
    "--timeout", "timeout_s", default=120.0, type=float, show_default=True,
    help="Max seconds to wait for launch.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def vp_create_cmd(
    ctx,
    stack_name,
    region,
    meeting_name,
    platform,
    meeting_id,
    password,
    user_name,
    wait,
    timeout_s,
    as_json,
):
    """Create and launch a Virtual Participant in a meeting."""
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)

    try:
        result = client.vp.create(
            meeting_name=meeting_name,
            platform=platform.upper(),
            meeting_id=meeting_id,
            meeting_password=password,
            user_name=user_name,
            wait=wait,
            timeout_s=timeout_s,
        )
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        console.print_json(json.dumps(result.model_dump(), indent=2, default=str))
        return

    print_success(f"Virtual Participant created: {result.id}")
    console.print(f"  Status:       [cyan]{result.status}[/cyan]")
    if result.call_id:
        console.print(f"  Call ID:      [cyan]{result.call_id}[/cyan]")
    console.print(f"  Meeting:      {result.meeting_name} ({result.meeting_platform} / {result.meeting_id})")
    console.print(f"  Elapsed:      {result.elapsed_ms:.1f} ms")
    if result.sfn_execution_arn:
        console.print(f"  SFN exec:     [dim]{result.sfn_execution_arn}[/dim]")


@vp_cmd.command("get")
@click.option(
    "--stack-name", default=None, envvar="LMA_STACK_NAME",
    help="CloudFormation stack name.",
)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--id", "vp_id", required=True, help="Virtual Participant ID.")
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def vp_get_cmd(ctx, stack_name, region, vp_id, as_json):
    """Fetch details for a specific Virtual Participant by id."""
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        row = client.vp.get(vp_id)
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        console.print_json(json.dumps(row.model_dump(), indent=2, default=str))
        return

    console.print(f"[bold]Virtual Participant:[/bold] {row.id}")
    console.print(f"  Meeting:   {row.meetingName} ({row.meetingPlatform} / {row.meetingId})")
    console.print(f"  Status:    [cyan]{row.status}[/cyan]")
    if row.CallId:
        console.print(f"  Call ID:   {row.CallId}")
    if row.updatedAt:
        console.print(f"  Updated:   [dim]{row.updatedAt}[/dim]")
    if row.vncEndpoint:
        console.print(f"  VNC:       {row.vncEndpoint}:{row.vncPort or ''} (ready={row.vncReady})")


@vp_cmd.command("end")
@click.option(
    "--stack-name", default=None, envvar="LMA_STACK_NAME",
    help="CloudFormation stack name.",
)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--id", "vp_id", required=True, help="Virtual Participant ID.")
@click.option("--reason", default="CLI requested termination", help="End reason.")
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def vp_end_cmd(ctx, stack_name, region, vp_id, reason, as_json):
    """End (stop) a running Virtual Participant."""
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        row = client.vp.end(vp_id, reason=reason, ended_by="lma-cli")
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        console.print_json(json.dumps(row.model_dump(), indent=2, default=str))
        return

    print_success(f"Virtual Participant ended: {row.id} → {row.status}")


@vp_cmd.command("list")
@click.option(
    "--stack-name", default=None, envvar="LMA_STACK_NAME",
    help="CloudFormation stack name.",
)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def vp_list_cmd(ctx, stack_name, region, as_json):
    """List Virtual Participants visible to the caller."""
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        rows = client.vp.list()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        console.print_json(
            json.dumps([r.model_dump() for r in rows], indent=2, default=str),
        )
        return

    if not rows:
        print_info("No Virtual Participants found.")
        return

    from rich.table import Table

    table = Table(title="Virtual Participants")
    table.add_column("ID", overflow="fold")
    table.add_column("Meeting")
    table.add_column("Platform")
    table.add_column("Status", style="cyan")
    table.add_column("Updated", style="dim")

    for row in rows:
        table.add_row(
            row.id or "",
            row.meetingName or "",
            row.meetingPlatform or "",
            row.status or "",
            row.updatedAt or "",
        )
    console.print(table)
