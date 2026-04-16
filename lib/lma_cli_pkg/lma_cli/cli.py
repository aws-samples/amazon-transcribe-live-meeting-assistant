# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA CLI — main entry point.

Usage::

    lma --help
    lma publish my-bucket lma us-east-1
    lma status
    lma outputs
    lma deploy --template-url <url>
    lma logs --list
    lma delete
"""

from __future__ import annotations

import os
import sys

import click

from lma_cli import __version__
from lma_cli.formatters import console, print_error


def _make_client(**overrides):
    """Create an LMAClient with CLI-level defaults + overrides."""
    from lma_sdk import LMAClient

    kwargs = {}
    if overrides.get("stack_name"):
        kwargs["stack_name"] = overrides["stack_name"]
    if overrides.get("region"):
        kwargs["region"] = overrides["region"]
    if overrides.get("profile"):
        kwargs["profile"] = overrides["profile"]
    return LMAClient(**kwargs)


# ── Main group ────────────────────────────────────────────────


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(version=__version__, prog_name="lma-cli")
@click.option("--region", envvar="AWS_DEFAULT_REGION", default=None, help="AWS region.")
@click.option("--profile", envvar="AWS_PROFILE", default=None, help="AWS CLI profile.")
@click.option("--stack-name", envvar="LMA_STACK_NAME", default=None, help="CloudFormation stack name.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable verbose logging.")
@click.pass_context
def main(ctx, region, profile, stack_name, verbose):
    """LMA CLI — AWS Live Meeting Assistant command-line interface.

    \b
    Environment Variables:
      LMA_STACK_NAME      Default stack name (default: LMA)
      AWS_DEFAULT_REGION   AWS region
      AWS_PROFILE          AWS CLI profile
    """
    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

    # Store a factory for lazy client creation (commands may override region/stack).
    # Use setdefault so tests can inject a mock factory via obj={"client_factory": ...}.
    ctx.ensure_object(dict)
    ctx.obj.setdefault("client_factory", lambda **kw: _make_client(
        region=kw.get("region") or region,
        profile=kw.get("profile") or profile,
        stack_name=kw.get("stack_name") or stack_name,
    ))
    ctx.obj["region"] = region
    ctx.obj["profile"] = profile
    ctx.obj["stack_name"] = stack_name

    # Show help if no subcommand
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ── Register commands ─────────────────────────────────────────

from lma_cli.commands.publish import check_prereqs_cmd, publish_cmd
from lma_cli.commands.stack import delete_cmd, deploy_cmd, logs_cmd, outputs_cmd, status_cmd

main.add_command(publish_cmd)
main.add_command(deploy_cmd)
main.add_command(status_cmd)
main.add_command(outputs_cmd)
main.add_command(delete_cmd)
main.add_command(logs_cmd)
main.add_command(check_prereqs_cmd)


if __name__ == "__main__":
    main()
