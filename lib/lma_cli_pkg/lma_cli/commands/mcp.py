# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""MCP server library commands: export a deployment's servers, import them into another.

Usage::

    lma mcp export --output mcp-servers.json
    lma --stack-name LMA-other mcp import mcp-servers.json

The export holds no credentials: each server's API keys, tokens and OAuth client
secret are empty placeholders, listed in its ``credentialsToReenter``. An
imported server that needs credentials is not used until an Admin enters them
on the MCP Servers page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from lma_cli.formatters import console, print_error, print_info, print_success


@click.group("mcp")
def mcp_cmd() -> None:
    """MCP server library (export, import)."""


@mcp_cmd.command("export")
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option(
    "--output", "-o", "output", default="-", show_default=True,
    help="File to write the library to; '-' writes it to standard output.",
)
@click.pass_context
def mcp_export_cmd(ctx, stack_name, region, output):
    """Export the installed MCP servers, without credentials."""
    from lma_sdk.exceptions import LMAError

    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        library = client.mcp.export_library()
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)
    text = json.dumps(library, indent=2) + "\n"
    if output == "-":
        click.echo(text, nl=False)
        return
    Path(output).write_text(text, encoding="utf-8")
    needing = sum(1 for s in library.get("servers", []) if s.get("credentialsToReenter"))
    print_success(f"Exported {len(library.get('servers', []))} MCP servers to {output}")
    if needing:
        print_info(f"{needing} of them need their credentials entered again after an import.")


@mcp_cmd.command("import")
@click.argument("library_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name.")
@click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
@click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")
@click.pass_context
def mcp_import_cmd(ctx, library_file, stack_name, region, as_json):
    """Create or update the MCP servers a library file lists, matched by name."""
    from lma_sdk.exceptions import LMAError

    try:
        library = json.loads(Path(library_file).read_text(encoding="utf-8"))
    except ValueError as e:
        print_error(f"{library_file} is not valid JSON: {e}")
        sys.exit(2)
    client = ctx.obj["client_factory"](stack_name=stack_name, region=region)
    try:
        result = client.mcp.import_library(library)
    except ValueError as e:
        print_error(str(e))
        sys.exit(2)
    except LMAError as e:
        print_error(str(e))
        sys.exit(1)
    if as_json:
        console.print_json(json.dumps(result, indent=2, default=str))
    else:
        for server in result.get("Servers") or []:
            note = " (credentials must be entered on the MCP Servers page)" if server.get("CredentialsRequired") else ""
            detail = f": {server['Message']}" if server.get("Message") else ""
            console.print(f"  {server.get('Name')}: {server.get('Action')}{note}{detail}")
        if result.get("Success"):
            print_success(result.get("Message") or "Imported")
        else:
            print_error(result.get("Message") or "The import failed")
    if not result.get("Success"):
        sys.exit(1)
