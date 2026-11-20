# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""The meeting assistant's skills: list, read, write, import and export them.

Usage::

    lma skills list
    lma skills get launch-review
    lma skills create --name launch-review --description "Review a launch plan." \\
        --instructions-file body.md --file reference/checklist.md=checklist.md
    lma skills update launch-review --description "Review a product launch plan."
    lma skills delete launch-review --yes
    lma skills import launch-review.zip            # or SKILL.md, or a Quick skill .md
    lma skills export launch-review --format zip --output launch-review.zip
    lma skills export-library --output skills.json
    lma --stack-name LMA-other skills import-library skills.json

A skill holds instructions and text reference files only; an import leaves out
scripts and other files that are not text, and lists them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import click

from lma_cli.formatters import console, print_error, print_info, print_success

_STACK = click.option(
    "--stack-name", default=None, envvar="LMA_STACK_NAME", help="CloudFormation stack name."
)
_REGION = click.option("--region", default=None, envvar="AWS_DEFAULT_REGION", help="AWS region.")
_JSON = click.option("--json", "as_json", is_flag=True, default=False, help="JSON output.")


def _client(ctx, stack_name, region):
    return ctx.obj["client_factory"](stack_name=stack_name, region=region)


def _fail(message: str, code: int = 1) -> None:
    print_error(message)
    sys.exit(code)


def _files(pairs: Tuple[str, ...]) -> Optional[Dict[str, str]]:
    """``--file path=local-file`` options as path -> text, or None when none was given."""
    if not pairs:
        return None
    files: Dict[str, str] = {}
    for pair in pairs:
        path, sep, local = pair.partition("=")
        if not sep or not path or not local:
            _fail(f"--file takes PATH=LOCAL_FILE, not {pair!r}", 2)
        files[path] = Path(local).read_text(encoding="utf-8")
    return files


def _metadata(text: Optional[str]):
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError as e:
        _fail(f"--metadata is not valid JSON: {e}", 2)
    if not isinstance(value, dict):
        _fail("--metadata must be a JSON object", 2)
    return value


@click.group("skills")
def skills_cmd() -> None:
    """The meeting assistant's skills (list, get, create, update, delete, import, export)."""


@skills_cmd.command("list")
@_STACK
@_REGION
@_JSON
@click.pass_context
def skills_list_cmd(ctx, stack_name, region, as_json):
    """List the skills."""
    from lma_sdk.exceptions import LMAError

    try:
        skills = _client(ctx, stack_name, region).skills.list()
    except LMAError as e:
        _fail(str(e))
    if as_json:
        console.print_json(json.dumps(skills, default=str))
        return
    if not skills:
        print_info("No skills.")
        return
    for skill in skills:
        files = len(skill.get("Files") or [])
        console.print(
            f"  {skill['SkillId']}: {skill.get('Description', '')} "
            f"({files} reference file{'s' if files != 1 else ''})"
        )


@skills_cmd.command("get")
@click.argument("skill_id")
@_STACK
@_REGION
@click.pass_context
def skills_get_cmd(ctx, skill_id, stack_name, region):
    """Show one skill, with its instructions and reference files, as JSON."""
    from lma_sdk.exceptions import LMAError

    try:
        skill = _client(ctx, stack_name, region).skills.get(skill_id)
    except LMAError as e:
        _fail(str(e))
    if skill is None:
        _fail(f"No skill {skill_id}")
    console.print_json(json.dumps(skill, default=str))


def _write_options(command):
    for option in reversed(
        [
            click.option("--description", default=None, help="When the skill applies."),
            click.option(
                "--instructions-file",
                type=click.Path(exists=True, dir_okay=False),
                default=None,
                help="Markdown file holding the instructions.",
            ),
            click.option("--display-name", default=None, help="Optional display name."),
            click.option(
                "--file",
                "file_pairs",
                multiple=True,
                help="Reference file as PATH=LOCAL_FILE (text only); repeat for several.",
            ),
            click.option("--metadata", default=None, help="Metadata as a JSON object."),
        ]
    ):
        command = option(command)
    return command


@skills_cmd.command("create")
@click.option("--name", required=True, help="Lowercase letters, digits and hyphens.")
@_write_options
@_STACK
@_REGION
@click.pass_context
def skills_create_cmd(
    ctx, name, description, instructions_file, display_name, file_pairs, metadata, stack_name, region
):
    """Create a skill."""
    from lma_sdk.exceptions import LMAError

    if not description or not instructions_file:
        _fail("--description and --instructions-file are required", 2)
    try:
        result = _client(ctx, stack_name, region).skills.create(
            name,
            description,
            Path(instructions_file).read_text(encoding="utf-8"),
            display_name=display_name,
            files=_files(file_pairs),
            metadata=_metadata(metadata),
        )
    except LMAError as e:
        _fail(str(e))
    print_success(f"Created skill {result.get('SkillId')}")


@skills_cmd.command("update")
@click.argument("skill_id")
@click.option("--name", default=None, help="A new name.")
@_write_options
@_STACK
@_REGION
@click.pass_context
def skills_update_cmd(
    ctx,
    skill_id,
    name,
    description,
    instructions_file,
    display_name,
    file_pairs,
    metadata,
    stack_name,
    region,
):
    """Change a skill; what is not given keeps its stored value (--file replaces every file)."""
    from lma_sdk.exceptions import LMAError

    try:
        _client(ctx, stack_name, region).skills.update(
            skill_id,
            name=name,
            description=description,
            instructions=(
                Path(instructions_file).read_text(encoding="utf-8") if instructions_file else None
            ),
            display_name=display_name,
            files=_files(file_pairs),
            metadata=_metadata(metadata),
        )
    except LMAError as e:
        _fail(str(e))
    print_success(f"Updated skill {skill_id}")


@skills_cmd.command("delete")
@click.argument("skill_id")
@click.option("--yes", is_flag=True, default=False, help="Do not ask for confirmation.")
@_STACK
@_REGION
@click.pass_context
def skills_delete_cmd(ctx, skill_id, yes, stack_name, region):
    """Delete a skill."""
    from lma_sdk.exceptions import LMAError

    if not yes:
        click.confirm(f"Delete skill {skill_id}?", abort=True)
    try:
        _client(ctx, stack_name, region).skills.delete(skill_id)
    except LMAError as e:
        _fail(str(e))
    print_success(f"Deleted skill {skill_id}")


@skills_cmd.command("import")
@click.argument("skill_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--replace", is_flag=True, default=False, help="Replace a skill of the same name.")
@_STACK
@_REGION
@_JSON
@click.pass_context
def skills_import_cmd(ctx, skill_file, replace, stack_name, region, as_json):
    """Import one skill from a SKILL.md, an Amazon Quick skill .md, or a .zip of a skill folder."""
    from lma_sdk.exceptions import LMAError

    try:
        result = _client(ctx, stack_name, region).skills.import_file(skill_file, overwrite=replace)
    except LMAError as e:
        _fail(str(e))
    if as_json:
        console.print_json(json.dumps(result, default=str))
        return
    print_success(result.get("Message") or "Imported")
    for skipped in result.get("Skipped") or []:
        console.print(f"  left out {skipped['Path']}: {skipped['Reason']}")
    for note in result.get("Notes") or []:
        print_info(note)


@skills_cmd.command("export")
@click.argument("skill_id")
@click.option(
    "--format",
    "file_format",
    type=click.Choice(["skill-md", "zip"]),
    default="skill-md",
    show_default=True,
)
@click.option("--output", "-o", default=None, help="File to write; default: the exported file's name.")
@_STACK
@_REGION
@click.pass_context
def skills_export_cmd(ctx, skill_id, file_format, output, stack_name, region):
    """Export one skill as a SKILL.md or a zip of its folder."""
    from lma_sdk.exceptions import LMAError

    import base64

    try:
        answer = _client(ctx, stack_name, region).skills.export(skill_id, file_format=file_format)
    except LMAError as e:
        _fail(str(e))
    target = Path(output or answer["fileName"])
    if answer.get("encoding") == "base64":
        target.write_bytes(base64.b64decode(answer["content"]))
    else:
        target.write_text(answer["content"], encoding="utf-8")
    print_success(f"Exported skill {skill_id} to {target}")


@skills_cmd.command("export-library")
@_STACK
@_REGION
@click.option(
    "--output",
    "-o",
    "output",
    default="-",
    show_default=True,
    help="File to write the library to; '-' writes it to standard output.",
)
@click.pass_context
def skills_export_library_cmd(ctx, stack_name, region, output):
    """Export every skill as one library file."""
    from lma_sdk.exceptions import LMAError

    try:
        library = _client(ctx, stack_name, region).skills.export_library()
    except LMAError as e:
        _fail(str(e))
    text = json.dumps(library, indent=2) + "\n"
    if output == "-":
        click.echo(text, nl=False)
        return
    Path(output).write_text(text, encoding="utf-8")
    print_success(f"Exported {len(library.get('skills', []))} skills to {output}")


@skills_cmd.command("import-library")
@click.argument("library_file", type=click.Path(exists=True, dir_okay=False))
@_STACK
@_REGION
@_JSON
@click.pass_context
def skills_import_library_cmd(ctx, library_file, stack_name, region, as_json):
    """Create or update the skills a library file lists, matched by name."""
    from lma_sdk.exceptions import LMAError

    try:
        library = json.loads(Path(library_file).read_text(encoding="utf-8"))
    except ValueError as e:
        _fail(f"{library_file} is not valid JSON: {e}", 2)
    client = _client(ctx, stack_name, region)
    try:
        result = client.skills.import_library(library)
    except ValueError as e:
        _fail(str(e), 2)
    except LMAError as e:
        _fail(str(e))
    if as_json:
        console.print_json(json.dumps(result, indent=2, default=str))
    else:
        for skill in result.get("Skills") or []:
            detail = f": {skill['Message']}" if skill.get("Message") else ""
            console.print(f"  {skill.get('Name')}: {skill.get('Action')}{detail}")
        if result.get("Success"):
            print_success(result.get("Message") or "Imported")
        else:
            print_error(result.get("Message") or "The import failed")
    if not result.get("Success"):
        sys.exit(1)
