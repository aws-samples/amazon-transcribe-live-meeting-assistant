# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meeting commands — reopen.

Usage::

    lma meetings reopen --id "<meeting ID>"
    lma meetings reopen --id "https://<web UI>/#/calls/<encoded meeting ID>"
"""

from __future__ import annotations

import json
import sys

import click

from lma_cli.formatters import console, print_error, print_info, print_success


@click.group("meetings")
def meetings_cmd() -> None:
    """LMA meetings."""


@meetings_cmd.command("reopen")
@click.option(
    "--stack-name", default=None, envvar="LMA_STACK_NAME",
    help="CloudFormation stack name.",
)
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option(
    "--id", "call_id", required=True,
    help="Meeting ID, or the link to the meeting's page in LMA.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def meetings_reopen_cmd(ctx, stack_name, region, call_id, as_json):
    """Allow one more capture session on an existing meeting.

    Prints the session number a streaming client names in its START frame
    (sessionNumber), with the meeting's ID, to continue the meeting, and where
    the session starts on the transcript timeline. Needs an assumed-role
    profile (SSO or role).
    """
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        session = client.meetings.reopen(call_id)
    except LMAError as e:
        error_type = getattr(e, "error_type", None)
        print_error(f"{e} ({error_type})" if error_type else str(e))
        sys.exit(1)

    if as_json:
        console.print_json(json.dumps(session.model_dump(), indent=2, default=str))
        return

    print_success(f"Meeting reopened: {session.call_id}")
    print_info(f"Session number: {session.session_number}")
    print_info(f"Session offset: {session.session_offset_seconds:g} s on the transcript timeline")
    print_info(
        "Stream with this meeting ID as callId and "
        f'"sessionNumber": {session.session_number} in the START frame.'
    )
