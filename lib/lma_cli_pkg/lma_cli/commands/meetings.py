# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meeting commands: delete the meetings that carry given labels.

Usage::

    lma meetings delete --label engagement=acme --user-name jdoe@example.com
    lma meetings delete --label engagement=acme --user-name jdoe@example.com --confirm

Without ``--confirm`` the command is a dry run: it lists the meetings it would
delete and changes nothing. ``--user-name`` is the LMA user it acts for: an Admin
deletes matching meetings of every user, anyone else only their own. Each meeting
is deleted through the same per-meeting delete path as the meetings list, so its
transcript, summary, key frames, recordings, consent record, notifications and
archives go with it. A meeting still in progress is skipped and listed.
"""

from __future__ import annotations

import sys

import click

from lma_cli.formatters import console, print_error, print_info, print_success, print_warning


@click.group("meetings")
def meetings_cmd() -> None:
    """Meetings (delete by label)."""


@meetings_cmd.command("delete")
@click.option(
    "--label", "labels", multiple=True, required=True, metavar="KEY=VALUE",
    help="A label the meetings carry; repeat it and every label has to match.",
)
@click.option(
    "--user-name", required=True, envvar="LMA_USER_NAME",
    help="The LMA user (email or Cognito user name) to act for: an Admin's selection is "
    "every user's meetings, anyone else's only their own.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Delete the meetings. Without it nothing is deleted: the meetings are listed.",
)
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def meetings_delete_cmd(ctx, labels, user_name, confirm, stack_name, region, as_json):
    """Delete every meeting with the given labels; a dry run unless --confirm is given."""
    from lma_sdk.exceptions import LMAError
    from lma_sdk.operations.meetings import parse_labels

    try:
        wanted = parse_labels(labels)
    except LMAError as e:
        print_error(str(e))
        sys.exit(2)
    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        result = client.meetings.delete_by_labels(wanted, as_user=user_name, confirm=confirm)
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)

    if as_json:
        click.echo(result.model_dump_json(indent=2))
        sys.exit(1 if result.failed else 0)

    selector = ", ".join(f"{key}={value}" for key, value in wanted.items())
    whose = "every user's meetings" if result.scope == "all" else f"meetings owned by {result.user}"
    print_info(f"Meetings labelled {selector}, among {whose}:")
    for meeting in result.matched:
        console.print(f"  {meeting.call_id}  (owner {meeting.owner}, created {meeting.created_at})")
    for meeting in result.skipped_in_progress:
        console.print(f"  {meeting.call_id}  (owner {meeting.owner}) skipped: still in progress")
    if result.skipped_in_progress:
        print_warning(
            f"{len(result.skipped_in_progress)} meeting(s) in progress are not deleted; "
            "run the command again after they end."
        )

    if result.dry_run:
        print_info(
            f"Dry run: {len(result.matched)} meeting(s) would be deleted. Nothing was deleted; "
            "run again with --confirm to delete them."
        )
        return
    for failure in result.failed:
        print_error(f"{failure.call_id}: {failure.error}")
    total = len(result.matched)
    if result.failed:
        print_error(f"Deleted {len(result.deleted)} of {total} meeting(s); {len(result.failed)} failed.")
        sys.exit(1)
    print_success(f"Deleted {len(result.deleted)} of {total} meeting(s).")
