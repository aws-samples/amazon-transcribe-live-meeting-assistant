# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""CLI command modules."""


def resolve_region(ctx, region):
    """Return the Region a subcommand should use.

    Every subcommand's ``--region`` falls back to ``AWS_DEFAULT_REGION``, and
    Click ranks an environment variable above a value inherited from the
    group, so ``lma --region us-east-1 deploy`` would otherwise run in the
    environment's Region. A ``--region`` given on the command line, to the
    subcommand or to ``lma`` itself, wins over the environment.
    """
    from click.core import ParameterSource

    if ctx.get_parameter_source("region") == ParameterSource.COMMANDLINE:
        return region
    obj = ctx.obj or {}
    if obj.get("region_from_command_line") and obj.get("region"):
        return obj["region"]
    return region
