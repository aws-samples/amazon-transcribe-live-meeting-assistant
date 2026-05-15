# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Click command tree for ``lma load ...``.

This module exposes a single ``load_command`` click group that's usable in
two ways:

1. Standalone:   ``lma-load <subcommand>`` (via the ``lma-load`` console script).
2. Plugged-in:   ``lma load <subcommand>`` when installed alongside
   ``lma-cli`` (via the ``lma_cli.plugins`` entry-point).
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from lma_load import __version__
from lma_load import cleanup as cleanup_mod
from lma_load import stack_info as stack_info_mod
from lma_load.observability.report import write_summary
from lma_load.run_context import RunContext, make_run_id
from lma_load.scenarios import backfill as backfill_mod
from lma_load.scenarios import concurrent as concurrent_mod
from lma_load.scenarios import rbac as rbac_mod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared option decorator — appears on every subcommand
# ---------------------------------------------------------------------------
# We accept both ``--stack-name`` (aligns with the main ``lma`` CLI) and
# ``--stack`` / ``-s`` (short-form for standalone ``lma-load`` use). The
# group-level ``--stack-name`` on the main ``lma`` CLI is inherited via
# ``ctx.parent`` so ``lma --stack-name FOO load backfill`` also works.
def _common_options(f):
    f = click.option(
        "--stack-name",
        "--stack",
        "-s",
        "stack",
        envvar="LMA_STACK_NAME",
        default=None,
        help="CloudFormation stack name (env: LMA_STACK_NAME). "
        "Also accepts `--stack` / `-s`.",
    )(f)
    f = click.option(
        "--region",
        envvar="AWS_DEFAULT_REGION",
        default=None,
        help="AWS region (env: AWS_DEFAULT_REGION).",
    )(f)

    f = click.option(
        "--profile",
        envvar="AWS_PROFILE",
        default=None,
        help="AWS CLI profile (env: AWS_PROFILE).",
    )(f)
    f = click.option(
        "--run-id",
        default=None,
        help="Explicit run-id to use (default: auto-generated).",
    )(f)
    f = click.option(
        "--results-dir",
        default="./results",
        type=click.Path(file_okay=False),
        help="Directory for per-run artifacts.",
    )(f)
    f = click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Print what would happen without creating/modifying AWS resources.",
    )(f)
    f = click.option(
        "--force",
        is_flag=True,
        default=False,
        help="Bypass large-scale confirmation prompts.",
    )(f)
    f = click.option(
        "-v",
        "--verbose",
        is_flag=True,
        default=False,
        help="Enable debug logging.",
    )(f)
    return f


def _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose):
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s: %(message)s",
        )

    if not stack:
        raise click.UsageError(
            "Stack name is required. Pass --stack-name, or set LMA_STACK_NAME."
        )

    # Region resolution order (matches boto3 / the `aws` CLI itself):
    #   1. explicit --region
    #   2. AWS_DEFAULT_REGION / AWS_REGION env (already picked up by click's envvar)
    #   3. the AWS config file's default (or the chosen --profile) region
    if not region:
        region = _default_region_from_profile(profile)
    if not region:
        raise click.UsageError(
            "Region is required. Pass --region, set AWS_DEFAULT_REGION, or "
            "configure a default region for your AWS profile "
            "(`aws configure set region <...>`)."
        )
    return RunContext(

        stack_name=stack,
        region=region,
        profile=profile,
        run_id=run_id or make_run_id(),
        results_dir=Path(results_dir),
        dry_run=dry_run,
        force=force,
    )


def _resolve_stack(ctx: RunContext):
    return stack_info_mod.resolve(ctx.stack_name, ctx.region, ctx.profile)


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------
@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="lma-load")
def load_command():
    """LMA Load Simulator — stress-test a deployed LMA stack.

    Scenarios:

    \b
      concurrent  — N in-flight meetings for Y duration
                    (--driver kinesis | upload | websocket | vp)
      backfill    — fabricate N historical meetings spread over Y days
      rbac        — provision N synthetic users + latency sweep
      cleanup     — remove every synthetic resource from a run-id

    Each run is tagged with a --run-id for deterministic cleanup later.
    """


# ---------------------------------------------------------------------------
# concurrent
# ---------------------------------------------------------------------------
@load_command.command("concurrent")
@_common_options
@click.option("--driver", type=click.Choice(["kinesis", "upload", "websocket", "vp"]),
              required=True, help="How each meeting is produced.")
@click.option("--meetings", type=int, required=True, help="Number of concurrent meetings.")
@click.option("--duration", default="5m",
              help="Per-meeting duration (e.g. 15m, 90s, 1h). Default: 5m.")
@click.option("--concurrency", type=int, default=0,
              help="Max in-flight (default = --meetings).")
@click.option("--ramp", default="30s", help="Spread starts across this window.")
@click.option("--jitter", default="2s", help="Extra per-meeting jitter.")
@click.option("--wav", "wav_path", default=None,
              help="Override the shipped WAV fixture (upload/websocket drivers).")
@click.option("--meeting-ids-file", default=None,
              help="YAML file of real meeting invites (vp driver).")
@click.option("--user-pool-size", type=int, default=0,
              help="Mint this many synthetic Cognito users (default = min(concurrency, meetings)).")
@click.option("--email-prefix", default=None,
              help="Prefix for synthetic-user emails, e.g. 'strahanr'.")
@click.option("--email-domain", default=None,
              help="Domain for synthetic-user emails, e.g. 'amazon.com'.")
def concurrent_cmd(
    stack, region, profile, run_id, results_dir, dry_run, force, verbose,
    driver, meetings, duration, concurrency, ramp, jitter,
    wav_path, meeting_ids_file, user_pool_size, email_prefix, email_domain,
):
    """Drive N concurrent meetings via a selectable driver."""
    # Validate driver-specific CLI argument dependencies up-front, BEFORE we
    # load AWS credentials and resolve the CloudFormation stack — both of
    # which are slow (seconds) and pointless if the command is going to fail
    # on arg validation anyway. The scenario module re-validates these plus
    # stack-resource preconditions later; this is purely a fail-fast for the
    # cheap, local checks.
    _validate_concurrent_args(
        driver=driver,
        meeting_ids_file=meeting_ids_file,
        wav_path=wav_path,
        email_prefix=email_prefix,
        email_domain=email_domain,
        user_pool_size=user_pool_size,
    )

    ctx = _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose)
    stack_info = _resolve_stack(ctx)

    params = concurrent_mod.ConcurrentParams(
        driver=driver,
        meetings=meetings,
        duration_s=_parse_duration(duration),
        concurrency=concurrency,
        ramp_s=_parse_duration(ramp),
        jitter_s=_parse_duration(jitter),
        wav_path=wav_path,
        meeting_ids_file=meeting_ids_file,
        user_pool_size=user_pool_size,
        email_prefix=email_prefix,
        email_domain=email_domain,
    )
    concurrent_mod.run(ctx, stack_info, params)
    write_summary(ctx.results_dir, ctx.run_id)


def _validate_concurrent_args(
    *,
    driver: str,
    meeting_ids_file: str | None,
    wav_path: str | None,
    email_prefix: str | None,
    email_domain: str | None,
    user_pool_size: int,
) -> None:
    """Raise ``click.UsageError`` for driver-specific arg mismatches.

    Keeps the error surface close to the user (argparse-style), rather than
    deep inside the scenario after AWS creds have been loaded and the stack
    has been walked. Each rule lives here so ``--help`` + this function are
    the single source of truth for what combinations are legal.
    """
    if driver == "vp":
        if not meeting_ids_file:
            raise click.UsageError(
                "--driver vp requires --meeting-ids-file <path> (YAML of real "
                "meeting invites; see utilities/load-simulator/examples/"
                "meetings.example.yaml). The vp driver joins each meeting via "
                "a real Chromium tab, so it needs genuine invite URLs."
            )
        p = Path(meeting_ids_file)
        if not p.is_file():
            raise click.UsageError(
                f"--meeting-ids-file not found: {meeting_ids_file!r}"
            )
        # The vp-loader iframe authenticates to AppSync/Cognito with a JWT,
        # so the driver mints a throwaway Cognito user per run. That needs
        # an email inbox we can reach via the `+alias` trick.
        if not (email_prefix and email_domain):
            raise click.UsageError(
                "--driver vp requires --email-prefix and --email-domain so a "
                "synthetic Cognito user can be minted and its JWT passed to "
                "the vp-loader iframe (usernames are "
                "`<prefix>+loadtest-<run-id>-NNNN@<domain>`)."
            )

    if driver in ("websocket", "upload"):
        # wav_path is optional (shipped fixture is the default), but if the
        # user DID pass one it needs to exist.
        if wav_path:
            p = Path(wav_path)
            if not p.is_file():
                raise click.UsageError(f"--wav file not found: {wav_path!r}")
        # Both drivers call AppSync/Kinesis as a real Cognito user so they
        # must mint synthetic users on the fly — that needs an email inbox
        # we can reach via the `+alias` trick.
        if not (email_prefix and email_domain):
            raise click.UsageError(
                f"--driver {driver} requires --email-prefix and --email-domain "
                "so synthetic Cognito users can be minted (usernames are "
                "`<prefix>+loadtest-<run-id>-NNNN@<domain>`)."
            )

    if driver == "websocket":
        if user_pool_size < 0:
            raise click.UsageError("--user-pool-size must be >= 0")

    # meeting_ids_file is only meaningful for vp; reject it elsewhere to
    # catch typos rather than silently ignore.
    if driver != "vp" and meeting_ids_file:
        raise click.UsageError(
            f"--meeting-ids-file is only valid with --driver vp "
            f"(got --driver {driver})."
        )


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------
# Long, multi-paragraph help shown for `lma load backfill --help`. Click re-wraps
# help text by paragraph; the `\b` literal tells click.formatting.wrap_text to
# preserve the raw line breaks for that paragraph. We use it around each
# OWNERSHIP MODES bullet and EXAMPLES snippet so indented code stays intact.
_BACKFILL_HELP = """\
Seed historical meetings (past timestamps) to stress the Meeting List, date-range
picker, and Bedrock summary throughput.

\b
OWNERSHIP MODES
  SYNTHETIC (default)
    --users N spreads ownership across N owner strings like
    `loadtest-<run-id>-u0001`. No Cognito calls; fast. Good for exercising
    the list paginator + RBAC filter logic, but NO real user can log in as
    these owners — so this mode does NOT verify what an end user sees under
    RBAC.

\b
  REAL COGNITO USERS (RBAC-at-scale)
    Pass --create-users together with --users N, --email-prefix and
    --email-domain to provision N real users in the stack's User Pool
    up-front and use their emails as the Owner values. Credentials are
    saved to `<results-dir>/cognito-users.json` so you can log in as any
    provisioned user and verify the Meeting List is correctly RBAC-filtered.
    --admin-fraction controls how many of the provisioned users are placed
    in the Admin group (default 20%).

\b
EXAMPLES:
\b
  # 500 meetings across 10 synthetic owners (fast, no Cognito):
  lma load backfill --stack my-lma --meetings 500 --users 10
\b
  # 500 meetings across 10 REAL users — RBAC-at-scale test.
  # Emails use the Gmail/SES '+alias' trick so all mail lands in your inbox:
  #   strahanr+loadtest-<run-id>-u0001@amazon.com
  lma load backfill --stack my-lma --meetings 500 --users 10 \\
      --create-users --email-prefix strahanr --email-domain amazon.com
\b
  # Same as above but 50% admins (admins see ALL meetings under RBAC):
  lma load backfill --stack my-lma --meetings 500 --users 10 \\
      --create-users --email-prefix strahanr --email-domain amazon.com \\
      --admin-fraction 0.5
"""


@load_command.command("backfill", help=_BACKFILL_HELP)
@_common_options
@click.option("--meetings", type=int, required=True, help="Total meetings to fabricate.")
@click.option("--days", type=int, default=30,
              help="Spread meetings uniformly over the last N days (default: 30).")
@click.option("--users", type=int, default=1,
              help=(
                  "Distribute ownership across N owners. In default (synthetic) mode "
                  "these are string-only owners; pass --create-users to provision N "
                  "real Cognito users instead. (default: 1)"
              ))
@click.option("--create-users", is_flag=True, default=False,
              help=(
                  "Provision real Cognito users in the stack's User Pool and use "
                  "their emails as the Owner values — enables RBAC-at-scale testing "
                  "(log in as any provisioned user and verify the filtered list). "
                  "Requires --email-prefix and --email-domain. Credentials written "
                  "to `<results-dir>/cognito-users.json`."
              ))
@click.option("--email-prefix", default=None,
              help=(
                  "Local-part of YOUR email inbox, e.g. 'strahanr'. Combined with "
                  "--email-domain to form the provisioned users' addresses: "
                  "`<prefix>+loadtest-<run-id>-<idx>@<domain>`. This is the Gmail "
                  "'+alias' trick — all mail lands in your real inbox so you can "
                  "click the Cognito invite/verification links. Required with "
                  "--create-users; ignored otherwise."
              ))
@click.option("--email-domain", default=None,
              help=(
                  "Domain of your email inbox, e.g. 'amazon.com'. See --email-prefix. "
                  "Required with --create-users; ignored otherwise."
              ))
@click.option("--admin-fraction", type=float, default=0.2,
              help=(
                  "Fraction of provisioned users placed in the Admin group "
                  "(default: 0.2 = 20%). Only used with --create-users. Admin users "
                  "see ALL meetings under RBAC; regular users see only their own."
              ))
@click.option("--skip-summary/--with-summary", default=False,
              help="Emit a real ADD_SUMMARY (via Bedrock) for every meeting. "
              "Default is --with-summary so Bedrock quotas get exercised. "
              "Pass --skip-summary to inject a deterministic synthetic "
              "summary instead (faster, ~$0, guarantees populated rows).")
@click.option("--direct-ddb", is_flag=True, default=False,
              help="[reserved] bypass Kinesis and batch-write directly to DDB.")
def backfill_cmd(
    stack, region, profile, run_id, results_dir, dry_run, force, verbose,
    meetings, days, users, create_users, email_prefix, email_domain,
    admin_fraction, skip_summary, direct_ddb,
):
    """Seed historical meetings (past timestamps) to stress list / date picker."""
    ctx = _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose)
    stack_info = _resolve_stack(ctx)
    params = backfill_mod.BackfillParams(
        meetings=meetings,
        days_back=days,
        user_count=users,
        email_prefix=email_prefix,
        email_domain=email_domain,
        create_cognito_users=create_users,
        admin_fraction=admin_fraction,
        skip_summary=skip_summary,
        direct_ddb=direct_ddb,
    )
    backfill_mod.run(ctx, stack_info, params)
    write_summary(ctx.results_dir, ctx.run_id)


# ---------------------------------------------------------------------------
# rbac
# ---------------------------------------------------------------------------
@load_command.command("rbac")
@_common_options
@click.option("--users", type=int, required=True, help="How many synthetic Cognito users to create.")
@click.option("--email-prefix", required=True, help="Local-part of your inbox, e.g. 'strahanr'.")
@click.option("--email-domain", required=True, help="Domain of your inbox, e.g. 'amazon.com'.")
@click.option("--iterations", type=int, default=20,
              help="listCalls queries per user (latency samples).")
@click.option("--admin-fraction", type=float, default=0.2,
              help="Fraction of users placed in the Admin group (default: 0.2).")
@click.option("--window-days", type=int, default=30,
              help="Lookback window for listCallsDateRange queries.")
def rbac_cmd(
    stack, region, profile, run_id, results_dir, dry_run, force, verbose,
    users, email_prefix, email_domain, iterations, admin_fraction, window_days,
):
    """Provision N users + latency-sweep listCalls under RBAC."""
    ctx = _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose)
    stack_info = _resolve_stack(ctx)
    params = rbac_mod.RbacParams(
        users=users,
        email_prefix=email_prefix,
        email_domain=email_domain,
        iterations=iterations,
        admin_fraction=admin_fraction,
        window_days=window_days,
    )
    rbac_mod.run(ctx, stack_info, params)
    write_summary(ctx.results_dir, ctx.run_id)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------
@load_command.command("cleanup")
@_common_options
@click.option(
    "--target-run-id",
    required=True,
    help="The run-id to clean up. Pass '*' to delete ALL load-simulator artifacts.",
)
def cleanup_cmd(
    stack, region, profile, run_id, results_dir, dry_run, force, verbose,
    target_run_id,
):
    """Delete every synthetic resource produced by a given run-id."""
    ctx = _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose)
    stack_info = _resolve_stack(ctx)
    cleanup_mod.run(ctx, stack_info, run_id_filter=target_run_id)
    write_summary(ctx.results_dir, ctx.run_id)


# ---------------------------------------------------------------------------
# stack-info helper
# ---------------------------------------------------------------------------
@load_command.command("stack-info")
@_common_options
def stackinfo_cmd(stack, region, profile, run_id, results_dir, dry_run, force, verbose):
    """Print the resolved stack resources — handy for debugging missing outputs."""
    ctx = _make_ctx(stack, region, profile, run_id, results_dir, dry_run, force, verbose)
    info = stack_info_mod.resolve(ctx.stack_name, ctx.region, ctx.profile)
    click.echo(
        "\n".join(
            [
                f"stack_name:           {info.stack_name}",
                f"region:               {info.region}",
                f"cloudfront_endpoint:  {info.cloudfront_endpoint}",
                f"ws_endpoint:          {info.ws_endpoint}",
                f"appsync_graphql_url:  {info.appsync_graphql_url}",
                f"user_pool_id:         {info.user_pool_id}",
                f"user_pool_client_id:  {info.user_pool_client_id}",
                f"call_data_stream:     {info.call_data_stream_name}",
                f"event_sourcing_table: {info.event_sourcing_table}",
                f"recordings_bucket:    {info.recordings_bucket}",
                f"vp_scheduler_sfn:     {info.vp_scheduler_state_machine_arn}",
                f"vp_ecs_cluster:       {info.vp_ecs_cluster_name}",
                f"vp_registry_table:    {info.vp_registry_table}",
            ]
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _default_region_from_profile(profile: str | None) -> str | None:
    """Return the region configured for the active / named AWS profile, or
    None if the AWS config doesn't specify one.

    We defer the boto3 import so ``--help`` renders quickly and so a user
    can still get help text without any AWS credentials present.
    """
    try:
        import boto3  # noqa: WPS433
    except ImportError:
        return None
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        return session.region_name
    except Exception:  # noqa: BLE001
        # Bad profile name, missing config file, etc. — the caller will
        # raise a friendlier click.UsageError below.
        return None


def _parse_duration(s: str) -> float:
    """Parse '15m', '90s', '1h' → seconds (float)."""

    if s is None:
        return 0.0
    s = str(s).strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


if __name__ == "__main__":
    load_command()
