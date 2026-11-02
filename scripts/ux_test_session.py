#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""
ux_test_session.py

Find a deployed LMA stack's web UI URL and, when a demo or review has to be seen
through a role other than your own, create (and later delete) a throwaway Cognito
user in the stack's user pool, so a recording never runs as, or risks the password
of, a real operator's account.

This is deliberately *only* setup and teardown. The browsing and what is shown or
said are the agent's job (see ``.claude/skills/product-demo.md``); putting them here
would mean a deterministic script pretending to know what a viewer needs to see.

Everything is read from the root LMA stack's CloudFormation outputs:

* ``ApplicationCloudfrontEndpoint`` is the web UI URL.
* ``CognitoUserPoolTokenIssuerUrl`` ends in the user pool id
  (``https://cognito-idp.<region>.amazonaws.com/<pool-id>``). The root stack has no
  output holding the pool id alone in every configuration, and this one is
  unconditional.

LMA has two roles (``docs/user-management.md``). ``Admin`` is the one Cognito group
``lma-cognito-stack`` creates; a user in no group is a regular ``User``, who sees
only their own meetings and those shared with them. ``--group`` accepts every group
the template creates, read from it so a new one is available without editing a list
here, plus ``User`` for the groupless role.

Usage:
  ./scripts/ux_test_session.py url <STACK_NAME> [--region us-east-1]
  ./scripts/ux_test_session.py setup <STACK_NAME> [--group Admin|User] [--region ...]
      [--email-domain example.invalid]
  ./scripts/ux_test_session.py teardown <STACK_NAME> --email <email> [--region ...]
  ./scripts/ux_test_session.py teardown <STACK_NAME> --stale [HOURS] [--region ...]

``setup`` prints a JSON blob with url / email / password / group and the exact
teardown command. ``teardown`` deletes the user.

The app client's auth flows are deliberately NOT modified. Setting a known password
non-interactively needs only ``admin-set-user-password --permanent``, which is an
admin API on the user pool and does not depend on the client's ``ExplicitAuthFlows``;
the browser then signs in over SRP, which the UI client already allows.

``--email-domain`` exists because a stack deployed with ``AllowedSignUpEmailDomain``
runs a pre-sign-up and a pre-authentication check that accept only addresses in
that domain, and both apply to an admin-created user too. The default,
``example.invalid``, is reserved by RFC 2606, so no message can reach a mailbox;
with another domain the local part is still a random ``ux-test-`` name, and the
invitation message is suppressed either way.

Always run teardown: setup and teardown are two invocations with an open-ended
session between them, and nothing else expires the account. ``teardown --stale``
sweeps throwaway users whose teardown was never run.

Requires the AWS CLI v2 on PATH (or ``AWS_CLI_BIN``) and credentials with
CloudFormation read and, for setup/teardown, Cognito user administration on the
stack's pool. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import string
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COGNITO_TEMPLATE = REPO_ROOT / "lma-cognito-stack" / "deployment" / "lma-cognito-stack.yaml"

#: The root stack outputs this script reads; checked against lma-main.yaml by the tests.
WEB_URL_OUTPUT = "ApplicationCloudfrontEndpoint"
ISSUER_URL_OUTPUT = "CognitoUserPoolTokenIssuerUrl"

#: The role of a user in no Cognito group.
GROUPLESS_ROLE = "User"
UX_TEST_PREFIX = "ux-test-"
DEFAULT_EMAIL_DOMAIN = "example.invalid"

AWS_BIN = os.environ.get("AWS_CLI_BIN") or "aws"


def aws(*args: str, region: str | None = None, stdin: str | None = None):
    """Run an AWS CLI command and return parsed JSON, or the raw text for
    ``--output text``. Raises RuntimeError with the CLI's stderr on failure.
    ``stdin`` is passed to the CLI's standard input, for ``--cli-input-json
    file:///dev/stdin``, so a value in it never appears in the process list."""
    cmd = [AWS_BIN, *args]
    if region:
        cmd += ["--region", region]
    res = subprocess.run(cmd, capture_output=True, text=True, input=stdin)  # nosec B603
    if res.returncode != 0:
        # Only the service and operation are named: the full argument list of
        # admin-set-user-password includes the password.
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {res.stderr.strip()}")
    out = res.stdout.strip()
    if "--output" in args and args[args.index("--output") + 1] == "text":
        return out
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def template_group_names(template_path: Path | None = None) -> list[str]:
    """Every ``AWS::Cognito::UserPoolGroup`` GroupName the Cognito template creates.

    A line scan rather than a YAML parse, because the template uses CloudFormation
    short-form tags that a plain YAML loader refuses, and this stays stdlib-only.
    ``Admin`` first, then sorted.
    """
    text = Path(template_path or COGNITO_TEMPLATE).read_text(encoding="utf-8")
    names: set[str] = set()
    for block in re.split(r"\n  (?=[A-Za-z0-9]+:\n)", text):
        if "Type: AWS::Cognito::UserPoolGroup" not in block:
            continue
        match = re.search(r"^\s+GroupName:\s*['\"]?([A-Za-z0-9_+=,.@-]+)", block, re.M)
        if match:
            names.add(match.group(1))
    return sorted(names, key=lambda n: (n != "Admin", n))


def role_names() -> list[str]:
    """What ``--group`` accepts: the template's groups, then the groupless role."""
    return [*template_group_names(), GROUPLESS_ROLE]


def resolve_stack(stack: str, region: str) -> dict:
    """The web UI URL and the user pool of a root LMA stack.

    Returns ``{stack, region, url, user_pool}``. Raises RuntimeError when the stack
    is not there (the CLI's message contains "does not exist"), when it is a nested
    stack, or when it lacks the outputs an LMA root stack has.
    """
    described = aws("cloudformation", "describe-stacks", "--stack-name", stack, region=region)
    stacks = (described or {}).get("Stacks") or []
    if not stacks:
        raise RuntimeError(f"Stack with id {stack} does not exist")
    info = stacks[0]
    if info.get("ParentId"):
        raise RuntimeError(
            f"{stack} is a nested stack. Pass the root LMA stack, the one you "
            "deployed, whose outputs include the web UI URL."
        )
    outputs = {o["OutputKey"]: o["OutputValue"] for o in info.get("Outputs") or []}
    url = outputs.get(WEB_URL_OUTPUT, "")
    issuer = outputs.get(ISSUER_URL_OUTPUT, "")
    if not url:
        raise RuntimeError(
            f"Stack {stack} has no {WEB_URL_OUTPUT} output. Is it a root LMA stack "
            "that finished deploying?"
        )
    user_pool = issuer.rstrip("/").rsplit("/", 1)[-1] if issuer else ""
    if not re.fullmatch(r"[\w-]+_[0-9A-Za-z]+", user_pool):
        raise RuntimeError(
            f"Could not read the user pool id from {stack}'s {ISSUER_URL_OUTPUT} "
            f"output ({issuer!r})."
        )
    return {"stack": stack, "region": region, "url": url, "user_pool": user_pool}


def create_cognito_user(ctx: dict, email: str, group: str | None, password: str) -> None:
    """A confirmed user with a permanent password, in ``group`` or in none.

    The invitation message is suppressed. A refusal from the pool's pre-sign-up
    check (an ``AllowedSignUpEmailDomain`` stack) is reported with what to do.
    """
    try:
        aws(
            "cognito-idp",
            "admin-create-user",
            "--user-pool-id",
            ctx["user_pool"],
            "--username",
            email,
            "--message-action",
            "SUPPRESS",
            "--user-attributes",
            f"Name=email,Value={email}",
            "Name=email_verified,Value=true",
            region=ctx["region"],
        )
    except RuntimeError as err:
        if "UserLambdaValidationException" in str(err):
            raise SystemExit(
                f"The user pool refused {email}: this stack accepts only addresses "
                "in its AllowedSignUpEmailDomain. Re-run setup with "
                "--email-domain <that domain>."
            ) from err
        raise
    aws(
        "cognito-idp",
        "admin-set-user-password",
        "--cli-input-json",
        "file:///dev/stdin",
        region=ctx["region"],
        stdin=json.dumps(
            {
                "UserPoolId": ctx["user_pool"],
                "Username": email,
                "Password": password,
                "Permanent": True,
            }
        ),
    )
    if group:
        aws(
            "cognito-idp",
            "admin-add-user-to-group",
            "--user-pool-id",
            ctx["user_pool"],
            "--username",
            email,
            "--group-name",
            group,
            region=ctx["region"],
        )


def delete_cognito_user(ctx: dict, email: str) -> None:
    """Delete a user. Best-effort (never raises), so it is safe on a cleanup path."""
    try:
        aws(
            "cognito-idp",
            "admin-delete-user",
            "--user-pool-id",
            ctx["user_pool"],
            "--username",
            email,
            region=ctx["region"],
        )
    except RuntimeError as err:
        print(f"Could not delete {email}: {err}", file=sys.stderr)


def _password() -> str:
    """A password satisfying the pool's policy, for a user that lives minutes."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(20))
    # Guarantee the character classes rather than hoping 20 random picks cover
    # them; a rejected password here fails the run for a silly reason.
    return f"Ux!{body}9aZ"


def _candidate_stacks(region: str) -> list[str]:
    """Root stacks in this region whose name mentions LMA, for a wrong-name error.

    Best-effort: if listing fails there are simply no suggestions to offer, which
    is no worse than the message without them.
    """
    try:
        names = aws(
            "cloudformation",
            "list-stacks",
            "--stack-status-filter",
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
            "--query",
            "StackSummaries[?ParentId==null].StackName",
            "--output",
            "text",
            region=region,
        )
    except Exception:  # noqa: BLE001 - a suggestion list is a nicety
        return []
    return [n for n in str(names).split() if "lma" in n.lower()]


def _resolve_or_explain(stack: str, region: str) -> dict:
    """resolve_stack, but a wrong stack name or region reads as an instruction.

    Wrong region is the likeliest cause and the hardest to spot, since the stack
    genuinely does not exist where you looked.
    """
    try:
        return resolve_stack(stack, region)
    except RuntimeError as err:
        if "does not exist" not in str(err):
            raise
        candidates = _candidate_stacks(region)
        lines = [
            f"Stack {stack!r} does not exist in {region}.",
            "",
            "The most common cause is the wrong --region: a stack is invisible from any other one.",
        ]
        if candidates:
            lines += ["", f"Stacks with LMA in their name in {region}:"]
            lines += [f"  {name}" for name in candidates]
        else:
            lines += [
                "",
                f"No stacks with LMA in their name in {region} — try another "
                "region, or check AWS_PROFILE (the credentials may be for a "
                "different account than the deployment).",
            ]
        raise SystemExit("\n".join(lines)) from err


def cmd_url(args: argparse.Namespace) -> int:
    """Just the web UI URL.

    The common case is attaching to your own already-signed-in browser, which
    needs no user and no teardown, only the address.
    """
    print(_resolve_or_explain(args.stack, args.region)["url"])
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    ctx = _resolve_or_explain(args.stack, args.region)
    domain = (getattr(args, "email_domain", None) or DEFAULT_EMAIL_DOMAIN).lstrip("@")
    email = f"{UX_TEST_PREFIX}{secrets.token_hex(4)}@{domain}"
    password = _password()
    group = None if args.group == GROUPLESS_ROLE else args.group

    # No change to the app client: see the module docstring. The admin APIs used
    # here need no client auth flow, and the browser signs in over SRP.
    create_cognito_user(ctx, email, group, password)

    teardown = (
        f"./scripts/ux_test_session.py teardown {args.stack} --email {email} --region {args.region}"
    )
    session = {
        "url": ctx["url"],
        "email": email,
        "password": password,
        "group": args.group,
        "stack": args.stack,
        "region": args.region,
        "teardown": teardown,
    }
    print(json.dumps(session, indent=2))
    return 0


def _stale_ux_users(
    ctx: dict, older_than_hours: float, domain: str = DEFAULT_EMAIL_DOMAIN
) -> list[str]:
    """Throwaway users in the pool older than ``older_than_hours``.

    Matched on the ``ux-test-`` prefix *and* the session domain, so a real
    operator account can never be selected however it is named.
    """
    result = aws(
        "cognito-idp",
        "list-users",
        "--user-pool-id",
        ctx["user_pool"],
        region=ctx["region"],
    )
    suffix = "@" + domain.lstrip("@").lower()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    stale = []
    for user in (result or {}).get("Users", []):
        name = user.get("Username", "")
        if not (name.startswith(UX_TEST_PREFIX) and name.lower().endswith(suffix)):
            continue
        created = user.get("UserCreateDate")
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created is None or created <= cutoff:
            stale.append(name)
    return stale


def cmd_teardown(args: argparse.Namespace) -> int:
    """Delete one throwaway user, or sweep abandoned ones with --stale."""
    email = getattr(args, "email", None)
    if args.stale is None and not email:
        raise SystemExit("teardown needs --email, or --stale to sweep abandoned users")
    if email and not str(email).lower().startswith(UX_TEST_PREFIX):
        raise SystemExit(
            f"{email} is not a {UX_TEST_PREFIX}* user made by setup; this command "
            "deletes only those."
        )
    ctx = _resolve_or_explain(args.stack, args.region)

    if args.stale is not None:
        domain = getattr(args, "email_domain", None) or DEFAULT_EMAIL_DOMAIN
        stale = _stale_ux_users(ctx, args.stale, domain)
        if not stale:
            print(f"No {UX_TEST_PREFIX} users older than {args.stale}h in {args.stack}.")
            return 0
        for name in stale:
            delete_cognito_user(ctx, name)
            print(f"Deleted stale {name}.")
        print(f"Swept {len(stale)} stale {UX_TEST_PREFIX} user(s).")
        return 0

    delete_cognito_user(ctx, email)
    print(f"Deleted {email}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    url = sub.add_parser("url", help="Print the stack's web UI URL and nothing else")
    url.add_argument("stack")
    url.add_argument("--region", default="us-east-1")
    url.set_defaults(func=cmd_url)

    setup = sub.add_parser("setup", help="Create a throwaway user to sign in as")
    setup.add_argument("stack")
    setup.add_argument(
        "--group",
        default="Admin",
        choices=role_names(),
        help=f"Cognito group, or {GROUPLESS_ROLE} for a user in no group (default Admin)",
    )
    setup.add_argument("--region", default="us-east-1")
    setup.add_argument(
        "--email-domain",
        default=DEFAULT_EMAIL_DOMAIN,
        help="domain of the throwaway address; set it to the stack's "
        "AllowedSignUpEmailDomain when it has one (default example.invalid)",
    )
    setup.set_defaults(func=cmd_setup)

    teardown = sub.add_parser("teardown", help="Delete a throwaway user")
    teardown.add_argument("stack")
    # Not required, because --stale is the other way to call this.
    teardown.add_argument("--email")
    teardown.add_argument(
        "--stale",
        nargs="?",
        type=float,
        const=12.0,
        default=None,
        metavar="HOURS",
        help=(
            "Instead of one user, delete every ux-test-* user on --email-domain "
            "older than HOURS (default 12): sweeps sessions whose teardown was "
            "never run"
        ),
    )
    teardown.add_argument("--email-domain", default=DEFAULT_EMAIL_DOMAIN)
    teardown.add_argument("--region", default="us-east-1")
    teardown.set_defaults(func=cmd_teardown)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
