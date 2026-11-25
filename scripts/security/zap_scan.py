#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OWASP ZAP scans of a deployed LMA stack: the AppSync GraphQL API, the web UI and the MCP API key endpoint.

Three targets, each its own ZAP run (official ``ghcr.io/zaproxy/zaproxy`` image):

``graphql``
    ``zap-api-scan.py -f graphql``, seeded with a plain SDL made from
    ``lma-ai-stack/source/appsync/schema.graphql``: the AppSync directives are
    dropped, the AWS scalars declared, and the Subscription type removed (ZAP
    sends HTTP requests only). Authenticated: a throwaway Admin signs in through
    SRP and its ID token is put on every request by ZAP's replacer, from a
    config file rather than the command line.
``web``
    ``zap-baseline.py`` against the CloudFront web UI: spider plus the passive
    rules (security headers, Content-Security-Policy, cookies, caching).
``mcp``
    ``zap-api-scan.py -f openapi`` against the MCP server's API key endpoint,
    seeded with a one-operation OpenAPI document (a JSON-RPC ``tools/list``),
    with a read key the throwaway Admin generates and revokes afterwards.
    Skipped when the stack has no MCP server.

**Passive by default.** ZAP's ``-S`` (safe mode) skips the active scan, and the
GraphQL seed holds the queries only, so a passive run sends no mutation. The
queries that call a model are left out of every run. ``--active`` sends attack
payloads, and the mutations too, so it is for a throwaway stack only: it can
change and delete that stack's data. The mutations that start paid work or
manage users are left out even then (``NEVER_SCANNED``).

**WARN-only.** A finding does not fail the run: the exit status is 0 when every
scan wrote its report, 2 when a scan produced none (the image could not be
pulled, ZAP did not start), 1 when the stack could not be read. Triage rules
are in ``zap-rules.conf`` beside this file.

Reports go to ``scratch/zap-reports/`` (ignored by git): ``<target>-report.html``,
``.json`` and ``.md`` from ZAP, and ``summary.json`` / ``summary.md`` with the
counts per target. The token file is deleted when the scans end.

Usage::

    AWS_PROFILE=default python3 scripts/security/zap_scan.py --stack-name LMA-dev --region us-west-2
    make stacktest-zap STACK=LMA-dev REGION=us-west-2 [ZAP_ACTIVE=1]

See docs/security-testing.md.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEG_TESTS = REPO_ROOT / "integ-tests"
SCHEMA_PATH = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "schema.graphql"
MATRIX_PATH = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "role-matrix.json"
RULES_PATH = Path(__file__).resolve().parent / "zap-rules.conf"
DEFAULT_REPORT_DIR = REPO_ROOT / "scratch" / "zap-reports"
ZAP_IMAGE = os.environ.get("ZAP_DOCKER_IMAGE", "ghcr.io/zaproxy/zaproxy:stable")
TARGETS = ("graphql", "web", "mcp")
SCAN_MINUTES = 30

AWS_SCALARS = (
    "AWSDate",
    "AWSTime",
    "AWSDateTime",
    "AWSTimestamp",
    "AWSEmail",
    "AWSJSON",
    "AWSURL",
    "AWSPhone",
    "AWSIPAddress",
)

# Root fields no scan sends. The queries call a model (cost, and a placeholder
# question tells nothing); the mutations start paid work (a Virtual
# Participant, a model, a troubleshooting run, an upload pipeline) or manage
# users, which an active scan on a stack's only Admin must not do.
NEVER_SCANNED = frozenset(
    {
        "Query.queryKnowledgeBase",
        "Query.parseMeetingInvitation",
        "Mutation.createVirtualParticipant",
        "Mutation.updateScheduledVirtualParticipant",
        "Mutation.rejoinVirtualParticipant",
        "Mutation.joinAgainVirtualParticipant",
        "Mutation.regenerateCallSummary",
        "Mutation.deriveCallAgenda",
        "Mutation.resolveSpeakerNames",
        "Mutation.requestMeetingDigest",
        "Mutation.requestWhiteboardUpdate",
        "Mutation.sendChatMessage",
        "Mutation.startTroubleshooting",
        "Mutation.createUploadMeeting",
        "Mutation.prepareMeetingArchive",
        "Mutation.enrolMyVoice",
        "Mutation.createUser",
        "Mutation.setUserRole",
        "Mutation.deleteUser",
    }
)

# The GraphQL add-on's generator: one request per root field, as a JSON POST,
# which is how AppSync takes them. Observed against a local echo server: the
# generator then sends each seeded query once, with its arguments inline and
# placeholder values, and the replacer's header on every request.
GRAPHQL_OPTIONS = {
    "graphql.querySplitType": "ROOT_FIELD",
    "graphql.requestMethod": "POST_JSON",
}

RISKS = {"3": "High", "2": "Medium", "1": "Low", "0": "Informational"}
TALLY_LABELS = ("FAIL-NEW", "FAIL-INPROG", "WARN-NEW", "WARN-INPROG", "INFO", "IGNORE", "PASS")


# ── the GraphQL seed ───────────────────────────────────────────────────────


def zap_sdl(
    schema_text: str, include_mutations: bool, exclude: frozenset[str] = NEVER_SCANNED
) -> str:
    """A plain SDL of the AppSync schema that ZAP's GraphQL parser accepts.

    Drops every ``@aws_*`` directive and its definition, declares the AWS
    scalars, removes the Subscription type (and the Mutation type unless
    ``include_mutations``), and removes the root fields named in ``exclude``
    ("Type.field"). The result parses and builds with plain graphql-core.
    """
    from graphql import (  # pylint: disable=import-outside-toplevel
        DirectiveDefinitionNode,
        DocumentNode,
        ObjectTypeDefinitionNode,
        ObjectTypeExtensionNode,
        SchemaDefinitionNode,
        parse,
        print_ast,
    )

    prelude = "\n".join(f"scalar {name}" for name in AWS_SCALARS)
    document = parse(prelude + "\n" + schema_text)
    dropped_roots = {"Subscription"} | (set() if include_mutations else {"Mutation"})

    def changed(node, **values):
        """The node with ``values`` set: a copy, since graphql-core 3.3 freezes its AST nodes."""
        if dataclasses.is_dataclass(node):
            return dataclasses.replace(node, **values)
        clone = copy.copy(node)
        for key, value in values.items():
            setattr(clone, key, value)
        return clone

    def strip(node):
        directives = getattr(node, "directives", None)
        if not directives:
            return node
        return changed(
            node, directives=tuple(d for d in directives if not d.name.value.startswith("aws_"))
        )

    definitions = []
    for definition in document.definitions:
        if isinstance(definition, DirectiveDefinitionNode) and definition.name.value.startswith(
            "aws_"
        ):
            continue
        if isinstance(definition, SchemaDefinitionNode):
            kept = tuple(
                t for t in definition.operation_types if t.type.name.value not in dropped_roots
            )
            definitions.append(changed(definition, operation_types=kept))
            continue
        if isinstance(definition, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
            name = definition.name.value
            if name in dropped_roots:
                continue
            fields = tuple(
                strip(field_node)
                for field_node in definition.fields or ()
                if f"{name}.{field_node.name.value}" not in exclude
            )
            definition = changed(definition, fields=fields)
        definitions.append(strip(definition))
    return print_ast(DocumentNode(definitions=tuple(definitions))) + "\n"


def mcp_openapi(endpoint: str) -> dict:
    """A one-operation OpenAPI document for the MCP API key endpoint: a JSON-RPC tools/list."""
    parts = urlsplit(endpoint)
    return {
        "openapi": "3.0.0",
        "info": {"title": "LMA MCP server, API key endpoint (ZAP seed)", "version": "1.0"},
        "servers": [{"url": f"{parts.scheme}://{parts.netloc}"}],
        "paths": {
            parts.path or "/": {
                "post": {
                    "operationId": "mcpJsonRpc",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object"},
                                "example": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            }
                        },
                    },
                    "responses": {"200": {"description": "JSON-RPC response"}},
                }
            }
        },
    }


def options_file(header: str, value: str, extra: dict[str, str] | None = None) -> str:
    """ZAP -configfile content: a replacer rule that sets ``header`` on every request, and any extra options."""
    lines = [
        "replacer.full_list(0).description=lma-auth",
        "replacer.full_list(0).enabled=true",
        "replacer.full_list(0).matchtype=REQ_HEADER",
        f"replacer.full_list(0).matchstr={header}",
        "replacer.full_list(0).regex=false",
        f"replacer.full_list(0).replacement={value}",
    ]
    lines += [f"{key}={val}" for key, val in (extra or {}).items()]
    return "\n".join(lines) + "\n"


# ── reading what ZAP wrote ─────────────────────────────────────────────────


def rule_tally(stdout: str) -> dict[str, int]:
    """The rule outcome counts ZAP prints last, e.g. ``FAIL-NEW: 0 ... PASS: 114``."""
    tally = {}
    for label in TALLY_LABELS:
        match = re.search(rf"{re.escape(label)}:\s*(\d+)", stdout or "")
        if match:
            tally[label] = int(match.group(1))
    return tally


def ignored_plugins(rules_text: str) -> set[str]:
    """Plugin ids marked IGNORE in a zap-rules.conf (``<id>\\t<action>\\t<comment>``)."""
    ids = set()
    for line in rules_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and not parts[0].startswith("#") and parts[1].upper() == "IGNORE":
            ids.add(parts[0])
    return ids


def parse_report(report: dict, ignore: set[str] | None = None) -> tuple[dict[str, int], list[dict]]:
    """Alert counts by risk and one entry per alert from a ZAP JSON report, the ignored plugins left out."""
    ignore = ignore or set()
    counts = {"High": 0, "Medium": 0, "Low": 0, "Informational": 0}
    alerts = []
    for site in report.get("site") or []:
        for alert in site.get("alerts") or []:
            plugin = str(alert.get("pluginid", ""))
            if plugin in ignore:
                continue
            risk = RISKS.get(str(alert.get("riskcode", "0")), "Informational")
            counts[risk] += 1
            instances = alert.get("instances") or []
            urls: list[str] = []
            for instance in instances:
                uri = instance.get("uri")
                if uri and uri not in urls:
                    urls.append(uri)
                if len(urls) == 3:
                    break
            alerts.append(
                {
                    "risk": risk,
                    "name": alert.get("alert") or alert.get("name") or "",
                    "pluginid": plugin,
                    "instances": len(instances) or int(alert.get("count") or 0),
                    "sampleUrls": urls,
                }
            )
    order = {"High": 0, "Medium": 1, "Low": 2, "Informational": 3}
    alerts.sort(key=lambda a: (order[a["risk"]], -a["instances"], a["name"]))
    return counts, alerts


@dataclass
class ScanResult:
    target: str
    url: str
    mode: str
    ran: bool = False
    skipped: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    tally: dict[str, int] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    error: str = ""


def summary_markdown(
    stack: str, results: list[ScanResult], when: str, show_alerts: bool = True
) -> str:
    lines = [
        "# LMA ZAP scan",
        "",
        f"Stack `{stack}`, {when}. WARN-only: findings are reported, they do not fail the run.",
        "",
        "| Target | Mode | High | Medium | Low | Info | Rules passed | Report |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if not r.ran:
            lines.append(
                f"| {r.target} | {r.mode} | - | - | - | - | - | not run: {r.skipped or r.error} |"
            )
            continue
        c = r.counts
        lines.append(
            f"| {r.target} | {r.mode} | {c.get('High', 0)} | {c.get('Medium', 0)} | {c.get('Low', 0)} "
            f"| {c.get('Informational', 0)} | {r.tally.get('PASS', '-')} | {r.target}-report.html |"
        )
    if show_alerts:
        for r in results:
            if r.alerts:
                lines += ["", f"## {r.target}", ""]
                lines += [
                    f"- [{a['risk']}] {a['name']} (plugin {a['pluginid']}, {a['instances']} instances)"
                    for a in r.alerts
                ]
    return "\n".join(lines) + "\n"


# ── running ZAP ────────────────────────────────────────────────────────────


def zap_on_path() -> bool:
    return shutil.which("zap-api-scan.py") is not None


def run_zap(
    script: str, args: list[str], workdir: Path, runner: str, timeout_minutes: int
) -> tuple[int, str]:
    """Run a ZAP packaged scan with ``workdir`` as its /zap/wrk; (exit status, output)."""
    if runner == "local":
        # Inside the ZAP image (the CI job): the scripts read and write /zap/wrk.
        wrk = Path("/zap/wrk")
        wrk.mkdir(parents=True, exist_ok=True)
        for item in workdir.iterdir():
            shutil.copy(item, wrk / item.name)
        command = [script, *args]
    else:
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workdir}:/zap/wrk:rw",
            ZAP_IMAGE,
            script,
            *args,
        ]
    try:
        # ZAP's scripts write their own log to the working directory, so inside the
        # image they run from /zap/wrk, which they can write.
        done = subprocess.run(  # noqa: S603 - fixed program, arguments built here
            command,
            capture_output=True,
            text=True,
            timeout=timeout_minutes * 60 + 300,
            check=False,
            cwd="/zap/wrk" if runner == "local" else None,
        )
        output = (done.stdout or "") + (done.stderr or "")
        status = done.returncode
    except subprocess.TimeoutExpired as expired:
        output, status = f"timed out: {expired}", 124
    if runner == "local":
        for item in Path("/zap/wrk").iterdir():
            if item.name.endswith(("-report.json", "-report.html", "-report.md")):
                shutil.copy(item, workdir / item.name)
    return status, output


def scan(
    target: str,
    script: str,
    args: list[str],
    workdir: Path,
    runner: str,
    result: ScanResult,
    ignore: set[str],
) -> None:
    status, output = run_zap(script, args, workdir, runner, SCAN_MINUTES)
    (workdir / f"{target}-zap.log").write_text(output, encoding="utf-8")
    report = workdir / f"{target}-report.json"
    if not report.exists():
        result.error = f"ZAP wrote no report (exit {status}); see {target}-zap.log"
        return
    result.ran = True
    result.tally = rule_tally(output)
    result.counts, result.alerts = parse_report(
        json.loads(report.read_text(encoding="utf-8")), ignore
    )
    result.reports = sorted(str(p) for p in workdir.glob(f"{target}-report.*"))


def zap_options(runner: str, target: str, config_file: str | None = None) -> str:
    """ZAP's own command-line options (``-z``): the scan's config file, and in the image its own home.

    Run inside the ZAP image, the scans share one user's home, and a second ZAP
    does not start while the first one's home is in use; each Docker run has a
    fresh one.
    """
    parts = [f"-configfile /zap/wrk/{config_file}"] if config_file else []
    if runner == "local":
        parts.append(f"-dir {Path.home() / f'.ZAP-{target}'}")
    return " ".join(parts)


def common_args(target: str, active: bool, safe_flag: bool = True) -> list[str]:
    """Report, rules and time flags; ``-S`` (skip the active scan) unless ``active``.

    zap-baseline.py is passive by definition and has no ``-S``, so the web target
    passes ``safe_flag=False``.
    """
    args = [
        "-J",
        f"{target}-report.json",
        "-r",
        f"{target}-report.html",
        "-w",
        f"{target}-report.md",
        "-I",
    ]
    args += ["-c", "zap-rules.conf", "-T", str(SCAN_MINUTES)]
    if safe_flag and not active:
        args.append("-S")
    return args


# ── the stack ──────────────────────────────────────────────────────────────


def _deployment(stack: str, region: str | None):
    sys.path.insert(0, str(INTEG_TESTS))
    import role_access_probe as rbac  # pylint: disable=import-outside-toplevel

    return rbac, rbac.Deployment.find(stack, region)


def main(argv: list[str] | None = None) -> int:  # noqa: C901 - one linear procedure
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--stack-name", required=True)
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    parser.add_argument(
        "--targets", default=",".join(TARGETS), help="comma-separated: graphql, web, mcp"
    )
    parser.add_argument(
        "--active",
        action="store_true",
        help="run ZAP's active scan and include mutations: for a throwaway stack only, it changes data",
    )
    parser.add_argument(
        "--report-dir", default=os.environ.get("LMA_ZAP_REPORT_DIR") or str(DEFAULT_REPORT_DIR)
    )
    parser.add_argument("--runner", choices=("auto", "docker", "local"), default="auto")
    parser.add_argument(
        "--counts-only",
        action="store_true",
        help="print counts, not alert names (for a CI job log)",
    )
    args = parser.parse_args(argv)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    unknown = set(targets) - set(TARGETS)
    if unknown:
        parser.error(f"unknown targets {sorted(unknown)}")
    runner = args.runner if args.runner != "auto" else ("local" if zap_on_path() else "docker")
    if runner == "docker" and shutil.which("docker") is None:
        print(
            "docker is not on PATH, and ZAP's scripts are not either; nothing to scan with.",
            file=sys.stderr,
        )
        return 2
    mode = "active" if args.active else "passive"
    if args.active:
        print(
            "ACTIVE scan: ZAP sends attack payloads and mutations to this stack's API. "
            "Use it on a throwaway stack only.",
            file=sys.stderr,
        )

    try:
        rbac, deployment = _deployment(args.stack_name, args.region)
    except Exception as error:  # noqa: BLE001 - reported, exit 1
        print(f"could not read stack {args.stack_name}: {error}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    report_dir = Path(args.report_dir)
    workdir = report_dir / stamp
    workdir.mkdir(parents=True, exist_ok=True)
    # The ZAP image runs as its own non-root user, which must write the reports here.
    os.chmod(workdir, 0o777)  # nosec B103 - a per-run report folder in an ignored scratch directory
    shutil.copy(RULES_PATH, workdir / "zap-rules.conf")
    ignore = ignored_plugins(RULES_PATH.read_text(encoding="utf-8"))

    results = {t: ScanResult(t, "", mode) for t in targets}
    secrets_written: list[Path] = []
    users = []
    key_prefix = ""
    try:
        users = rbac.create_role_users(deployment, stamp, (("zap", "Admin"),))
        rbac.sign_in(deployment, users[0])
        admin = users[0]

        if "graphql" in results:
            result = results["graphql"]
            result.url = deployment.graphql_url
            include = args.active
            (workdir / "schema.graphql").write_text(
                zap_sdl(SCHEMA_PATH.read_text(encoding="utf-8"), include_mutations=include),
                encoding="utf-8",
            )
            prop = workdir / "graphql-options.prop"
            prop.write_text(
                options_file("Authorization", admin.tokens["id"], GRAPHQL_OPTIONS), encoding="utf-8"
            )
            secrets_written.append(prop)
            print(f"ZAP graphql ({mode}): {deployment.graphql_url}")
            scan(
                "graphql",
                "zap-api-scan.py",
                [
                    "-t",
                    deployment.graphql_url,
                    "-f",
                    "graphql",
                    "--schema",
                    "/zap/wrk/schema.graphql",
                    "-z",
                    zap_options(runner, "graphql", "graphql-options.prop"),
                    *common_args("graphql", args.active),
                ],
                workdir,
                runner,
                result,
                ignore,
            )

        if "web" in results:
            result = results["web"]
            result.mode = "passive"
            result.url = deployment.outputs["ApplicationCloudfrontEndpoint"]
            print(f"ZAP web (passive baseline): {result.url}")
            scan(
                "web",
                "zap-baseline.py",
                [
                    "-t",
                    result.url,
                    "-m",
                    "2",
                    *(["-z", zap_options(runner, "web")] if runner == "local" else []),
                    *common_args("web", False, safe_flag=False),
                ],
                workdir,
                runner,
                result,
                ignore,
            )

        if "mcp" in results:
            result = results["mcp"]
            endpoint = deployment.outputs.get("MCPServerApiKeyEndpoint")
            if not endpoint:
                result.skipped = "the stack has no MCP server (EnableMCP=false)"
            else:
                result.url = endpoint
                sys.path.insert(0, str(INTEG_TESTS))
                import security_access_probe as sap  # pylint: disable=import-outside-toplevel

                status, body = sap.as_user(deployment, admin, sap.GENERATE_KEY, {"s": "read"})
                generated = (body.get("data") or {}).get("generateMCPApiKey") or {}
                if not generated:
                    result.error = f"could not generate an MCP API key (HTTP {status})"
                else:
                    key_prefix = generated["keyPrefix"]
                    (workdir / "mcp-openapi.json").write_text(
                        json.dumps(mcp_openapi(endpoint)), encoding="utf-8"
                    )
                    prop = workdir / "mcp-options.prop"
                    prop.write_text(
                        options_file("x-api-key", generated["keyValue"]), encoding="utf-8"
                    )
                    secrets_written.append(prop)
                    print(f"ZAP mcp ({mode}): {endpoint}")
                    scan(
                        "mcp",
                        "zap-api-scan.py",
                        [
                            "-t",
                            "/zap/wrk/mcp-openapi.json",
                            "-f",
                            "openapi",
                            "-z",
                            zap_options(runner, "mcp", "mcp-options.prop"),
                            *common_args("mcp", args.active),
                        ],
                        workdir,
                        runner,
                        result,
                        ignore,
                    )
    finally:
        for path in secrets_written:
            path.unlink(missing_ok=True)
            if runner == "local":
                Path("/zap/wrk", path.name).unlink(missing_ok=True)
        if key_prefix and users:
            sys.path.insert(0, str(INTEG_TESTS))
            import security_access_probe as sap  # pylint: disable=import-outside-toplevel

            sap.as_user(deployment, users[0], sap.REVOKE_KEY, {"p": key_prefix})
        left = rbac.delete_role_users(deployment, users) if users else []
        if left:
            print(f"could not delete the temporary users {left}", file=sys.stderr)

    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ordered = [results[t] for t in targets]
    (workdir / "summary.json").write_text(
        json.dumps(
            {
                "stack": args.stack_name,
                "generatedAt": when,
                "mode": mode,
                "results": [r.__dict__ for r in ordered],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (workdir / "summary.md").write_text(
        summary_markdown(args.stack_name, ordered, when), encoding="utf-8"
    )
    latest = report_dir / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(workdir.name)

    print(summary_markdown(args.stack_name, ordered, when, show_alerts=not args.counts_only))
    print(f"Reports: {workdir}")
    missing = [r.target for r in ordered if not r.ran and not r.skipped]
    if missing:
        print(f"no report from: {', '.join(missing)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
