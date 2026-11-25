# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""User commands: list a deployment's users for a user access review.

Usage::

    lma users list
    lma users list --format csv --output lma-users.csv
    lma users list --format json

Each user is listed with email, role (Admin, User or Participant), Cognito status, whether the
account is enabled, when it was created, when the user last signed in and when
they were last active. The last sign-in is the last interactive sign-in the
Cognito stack recorded. The last activity is the latest sign-in, token renewal
(about hourly while a client is open) or MCP API key use, each recorded at most
once per 15 minutes. A user with nothing recorded shows "Not recorded". When the
times cannot be read at all they show "Unavailable", with a warning that says why.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import click

from lma_cli.commands.meetings import _explicit
from lma_cli.formatters import console, err_console, print_error, print_info, print_warning

#: What a user with no recorded sign-in shows, and what every user shows when the
#: times could not be read.
NOT_RECORDED = "Not recorded"
UNAVAILABLE = "Unavailable"

CSV_COLUMNS = (
    "email",
    "username",
    "role",
    "status",
    "enabled",
    "created_at",
    "last_sign_in_at",
    "last_active_at",
)


def last_sign_in_text(user, available: bool) -> str:
    """The last sign-in as shown in the table and the CSV file."""
    if not available:
        return UNAVAILABLE
    return user.last_sign_in_at or NOT_RECORDED


def last_active_text(user, available: bool) -> str:
    """The last activity as shown in the table and the CSV file."""
    if not available:
        return UNAVAILABLE
    return user.last_active_at or NOT_RECORDED


def _cell(iso: str | None, available: bool) -> str:
    """A recorded time for the table, or what stands in for one."""
    if available and iso:
        return _shown(iso)
    return UNAVAILABLE if not available else NOT_RECORDED


def _shown(iso: str | None) -> str:
    """``2026-11-20T09:30:00.000Z`` as ``2026-11-20 09:30:00 UTC``, for the table."""
    if not iso:
        return "-"
    return iso.replace("T", " ").replace(".000Z", "").rstrip("Z") + " UTC"


def to_csv(result) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for user in result.users:
        writer.writerow(
            [
                user.email,
                user.username,
                user.role,
                user.status,
                "true" if user.enabled else "false",
                user.created_at or "",
                last_sign_in_text(user, result.last_sign_in_available),
                last_active_text(user, result.last_sign_in_available),
            ]
        )
    return out.getvalue()


@click.group("users")
def users_cmd() -> None:
    """Users (list them for an access review)."""


@users_cmd.command("list")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "csv"]),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the json or csv output to this file instead of standard output.",
)
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.pass_context
def users_list_cmd(ctx, output_format, output, stack_name, region):
    """List every user with email, role, status, enabled, created, last sign-in and last active."""
    from lma_sdk.exceptions import LMAError

    if output is not None and output_format == "table":
        print_error("--output writes json or csv: add --format json or --format csv.")
        sys.exit(2)
    # As for `lma meetings`: a stack or Region given before `users` wins over this
    # command's own option when that came only from the environment.
    stack_name = _explicit(ctx, "stack_name", stack_name)
    region = _explicit(ctx, "region", region)
    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        result = client.users.list()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if not result.last_sign_in_available:
        # On standard error, so a redirected json or csv file stays clean.
        err_console.print(
            f"[warning]⚠[/warning] Activity times are unavailable: "
            f"{result.last_sign_in_unavailable_reason}"
        )

    if output_format == "json":
        _emit(result.model_dump_json(indent=2) + "\n", output)
        return
    if output_format == "csv":
        _emit(to_csv(result), output)
        return

    from rich.table import Table

    account = f", account {result.account}" if result.account else ""
    print_info(f"Stack {result.stack} (Region {result.region}{account}), user pool {result.user_pool_id}")
    table = Table(title=f"Users ({len(result.users)})")
    table.add_column("Email", overflow="fold")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Enabled")
    table.add_column("Created", style="dim")
    table.add_column("Last sign-in")
    table.add_column("Last active")
    available = result.last_sign_in_available
    for user in result.users:
        table.add_row(
            user.email,
            user.role,
            user.status,
            "Yes" if user.enabled else "No",
            _shown(user.created_at),
            _cell(user.last_sign_in_at, available),
            _cell(user.last_active_at, available),
        )
    console.print(table)
    if result.last_sign_in_available:
        print_info(
            "Last sign-in is the last interactive sign-in; token refreshes are not counted. "
            "Last active also counts token renewals (about hourly while a client is open) "
            "and MCP API key use, to within 15 minutes. "
            f'"{NOT_RECORDED}": nothing recorded since recording began.'
        )
    else:
        print_warning("Last sign-in and last active times could not be read; see the warning above.")


def _emit(text: str, output: Path | None) -> None:
    if output is None:
        click.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8")
    print_info(f"Wrote {output}")
