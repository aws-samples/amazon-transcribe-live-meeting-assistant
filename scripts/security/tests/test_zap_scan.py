# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of zap_scan.py: the GraphQL seed, the MCP seed, the rules file and the report parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zap_scan  # noqa: E402

graphql = pytest.importorskip("graphql")

APPSYNC_PRELUDE = (
    "\n".join(f"scalar {name}" for name in zap_scan.AWS_SCALARS)
    + """
directive @aws_subscribe(mutations: [String!]!) on FIELD_DEFINITION
directive @aws_auth(cognito_groups: [String!]) on FIELD_DEFINITION
directive @aws_cognito_user_pools(cognito_groups: [String!]) on OBJECT | FIELD_DEFINITION
directive @aws_iam on OBJECT | FIELD_DEFINITION
directive @aws_api_key on OBJECT | FIELD_DEFINITION
directive @aws_oidc on OBJECT | FIELD_DEFINITION
directive @aws_lambda on OBJECT | FIELD_DEFINITION
"""
)


@pytest.fixture(scope="module")
def appsync_schema():
    text = zap_scan.SCHEMA_PATH.read_text(encoding="utf-8")
    return graphql.build_ast_schema(graphql.parse(APPSYNC_PRELUDE + text), assume_valid_sdl=True)


@pytest.fixture(scope="module", params=[False, True], ids=["passive", "active"])
def seed(request):
    text = zap_scan.zap_sdl(
        zap_scan.SCHEMA_PATH.read_text(encoding="utf-8"), include_mutations=request.param
    )
    return request.param, text, graphql.build_ast_schema(graphql.parse(text))


# ── the GraphQL seed ───────────────────────────────────────────────────────


def test_the_seed_is_plain_sdl_that_builds_without_appsyncs_definitions(seed) -> None:
    _, text, schema = seed
    assert "@aws_" not in text and "directive @" not in text
    assert schema.query_type is not None
    assert graphql.validate_schema(schema) == []


def test_the_seed_declares_every_aws_scalar_the_schema_uses(seed) -> None:
    _, _, schema = seed
    used = {name for name in zap_scan.AWS_SCALARS if name in zap_scan.SCHEMA_PATH.read_text()}
    assert used and all(isinstance(schema.get_type(n), graphql.GraphQLScalarType) for n in used)


def test_the_seed_never_holds_subscriptions_or_the_fields_no_scan_sends(seed) -> None:
    _, _, schema = seed
    assert schema.subscription_type is None
    for operation in zap_scan.NEVER_SCANNED:
        type_name, field_name = operation.split(".")
        root = schema.get_type(type_name)
        assert root is None or field_name not in root.fields, operation


def test_a_passive_seed_holds_the_queries_only(seed, appsync_schema) -> None:
    active, _, schema = seed
    queries = set(appsync_schema.query_type.fields)
    expected = {q for q in queries if f"Query.{q}" not in zap_scan.NEVER_SCANNED}
    assert set(schema.query_type.fields) == expected
    if active:
        mutations = {
            m
            for m in appsync_schema.mutation_type.fields
            if f"Mutation.{m}" not in zap_scan.NEVER_SCANNED
        }
        assert set(schema.mutation_type.fields) == mutations
    else:
        assert schema.mutation_type is None


def test_every_field_no_scan_sends_is_a_field_of_the_schema(appsync_schema) -> None:
    """A rename would otherwise drop a field from the list silently, and the scan would send it."""
    for operation in zap_scan.NEVER_SCANNED:
        type_name, field_name = operation.split(".")
        assert field_name in appsync_schema.get_type(type_name).fields, operation


def test_the_queries_that_call_a_model_are_never_scanned() -> None:
    """The role matrix names them for the role probe; the ZAP scan leaves out the same ones."""
    matrix = json.loads(zap_scan.MATRIX_PATH.read_text(encoding="utf-8"))
    assert set(matrix["probe"]["skipAllowed"]) <= zap_scan.NEVER_SCANNED


def test_the_seed_keeps_argument_and_field_types(seed, appsync_schema) -> None:
    _, _, schema = seed
    original = appsync_schema.query_type.fields["getCall"]
    stripped = schema.query_type.fields["getCall"]
    assert str(stripped.type) == str(original.type)
    assert {n: str(a.type) for n, a in stripped.args.items()} == {
        n: str(a.type) for n, a in original.args.items()
    }


# ── the MCP seed and the options file ──────────────────────────────────────


def test_the_mcp_seed_is_one_post_on_the_endpoint_path() -> None:
    spec = zap_scan.mcp_openapi("https://abc.execute-api.us-west-2.amazonaws.com/prod/mcp")
    assert spec["servers"] == [{"url": "https://abc.execute-api.us-west-2.amazonaws.com"}]
    assert list(spec["paths"]) == ["/prod/mcp"]
    example = spec["paths"]["/prod/mcp"]["post"]["requestBody"]["content"]["application/json"][
        "example"
    ]
    assert example["method"] == "tools/list"


def test_the_options_file_sets_the_header_with_a_replacer_rule() -> None:
    text = zap_scan.options_file("Authorization", "eyJ.token.sig", {"graphql.argsType": "BOTH"})
    lines = text.splitlines()
    assert "replacer.full_list(0).matchtype=REQ_HEADER" in lines
    assert "replacer.full_list(0).matchstr=Authorization" in lines
    assert "replacer.full_list(0).replacement=eyJ.token.sig" in lines
    assert "graphql.argsType=BOTH" in lines


def test_the_graphql_options_post_json_one_root_field_at_a_time() -> None:
    assert zap_scan.GRAPHQL_OPTIONS["graphql.requestMethod"] == "POST_JSON"
    assert zap_scan.GRAPHQL_OPTIONS["graphql.querySplitType"] == "ROOT_FIELD"


# ── the rules file ─────────────────────────────────────────────────────────


def test_every_rule_line_is_an_id_a_tab_an_action_and_a_tab() -> None:
    """ZAP reads the file as tab-separated: a line with spaces is ignored without a word."""
    rules = [
        line
        for line in zap_scan.RULES_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules
    for line in rules:
        parts = line.split("\t")
        assert (
            len(parts) == 3
            and parts[0].isdigit()
            and parts[1] in ("IGNORE", "WARN", "FAIL", "INFO")
        ), line


def test_ignored_plugins_are_read_from_the_rules_file() -> None:
    ignored = zap_scan.ignored_plugins(zap_scan.RULES_PATH.read_text())
    assert {"10096", "10027"} <= ignored
    assert "10038" not in ignored
    assert zap_scan.ignored_plugins("# 1\tIGNORE\n2\tWARN\t(x)\n3\tignore\t(y)\n") == {"3"}


# ── reading what ZAP wrote ─────────────────────────────────────────────────


def test_the_rule_tally_is_read_from_zaps_last_line() -> None:
    out = "...\nFAIL-NEW: 0\tFAIL-INPROG: 0\tWARN-NEW: 3\tWARN-INPROG: 0\tINFO: 1\tIGNORE: 2\tPASS: 114\n"
    assert zap_scan.rule_tally(out) == {
        "FAIL-NEW": 0,
        "FAIL-INPROG": 0,
        "WARN-NEW": 3,
        "WARN-INPROG": 0,
        "INFO": 1,
        "IGNORE": 2,
        "PASS": 114,
    }
    assert zap_scan.rule_tally("ZAP did not start") == {}


def _report() -> dict:
    def instances(*uris):
        return [{"uri": u} for u in uris]

    return {
        "site": [
            {
                "alerts": [
                    {
                        "pluginid": "10038",
                        "alert": "Header A",
                        "riskcode": "2",
                        "instances": instances("u1", "u1", "u2", "u3", "u4"),
                    },
                    {
                        "pluginid": "10096",
                        "alert": "Ignored",
                        "riskcode": "0",
                        "instances": instances("u1"),
                    },
                    {
                        "pluginid": "10021",
                        "alert": "Header B",
                        "riskcode": "1",
                        "count": "7",
                        "instances": [],
                    },
                ]
            },
            {
                "alerts": [
                    {
                        "pluginid": "40012",
                        "alert": "High one",
                        "riskcode": "3",
                        "instances": instances("u9"),
                    }
                ]
            },
        ]
    }


def test_alerts_are_counted_by_risk_with_the_ignored_plugins_left_out() -> None:
    counts, alerts = zap_scan.parse_report(_report(), {"10096"})
    assert counts == {"High": 1, "Medium": 1, "Low": 1, "Informational": 0}
    assert [a["name"] for a in alerts] == ["High one", "Header A", "Header B"]
    assert alerts[1]["sampleUrls"] == ["u1", "u2", "u3"]
    assert alerts[1]["instances"] == 5 and alerts[2]["instances"] == 7
    counts, _ = zap_scan.parse_report(_report())
    assert counts["Informational"] == 1


def test_the_summary_shows_counts_and_names_alerts_only_when_asked() -> None:
    ran = zap_scan.ScanResult(
        "graphql",
        "https://x",
        "passive",
        ran=True,
        counts={"High": 0, "Medium": 2},
        tally={"PASS": 90},
    )
    ran.alerts = [{"risk": "Medium", "name": "Header A", "pluginid": "10038", "instances": 2}]
    skipped = zap_scan.ScanResult("mcp", "", "passive", skipped="the stack has no MCP server")
    text = zap_scan.summary_markdown("LMA-x", [ran, skipped], "now")
    assert "| graphql | passive | 0 | 2 | 0 | 0 | 90 | graphql-report.html |" in text
    assert "not run: the stack has no MCP server" in text
    assert "Header A" in text
    assert "Header A" not in zap_scan.summary_markdown(
        "LMA-x", [ran, skipped], "now", show_alerts=False
    )


def test_a_passive_scan_passes_zaps_safe_mode_flag_and_an_active_one_does_not() -> None:
    assert "-S" in zap_scan.common_args("graphql", active=False)
    assert "-S" not in zap_scan.common_args("graphql", active=True)
    # -I: warnings do not fail ZAP's exit status.
    assert "-I" in zap_scan.common_args("web", active=False)
    # zap-baseline.py is passive and refuses -S.
    assert "-S" not in zap_scan.common_args("web", active=False, safe_flag=False)


def test_the_ci_job_and_the_script_use_the_same_pinned_zap_release() -> None:
    import yaml

    job = yaml.safe_load((zap_scan.REPO_ROOT / ".gitlab-ci.yml").read_text())["zap_dast"]
    if "ZAP_DOCKER_IMAGE" not in __import__("os").environ:
        assert job["image"] == zap_scan.ZAP_IMAGE
    assert "@sha256:" in job["image"] and ":stable" not in job["image"]


def test_the_ci_job_runs_by_hand_on_develop_only() -> None:
    import yaml

    job = yaml.safe_load((zap_scan.REPO_ROOT / ".gitlab-ci.yml").read_text())["zap_dast"]
    manual = [r for r in job["rules"] if r.get("when") == "manual"]
    assert manual and all('$CI_COMMIT_BRANCH == "develop"' in r["if"] for r in manual)


def test_the_ci_job_installs_pinned_packages() -> None:
    import yaml

    job = yaml.safe_load((zap_scan.REPO_ROOT / ".gitlab-ci.yml").read_text())["zap_dast"]
    assert "-r scripts/security/requirements-zap-job.txt" in " ".join(job["before_script"])
    path = zap_scan.REPO_ROOT / "scripts/security/requirements-zap-job.txt"
    lines = [line.strip() for line in path.read_text().splitlines()]
    packages = [line for line in lines if line and not line.startswith("#")]
    assert packages and all("==" in line for line in packages)


def test_unknown_targets_are_refused(capsys) -> None:
    with pytest.raises(SystemExit):
        zap_scan.main(["--stack-name", "x", "--targets", "graphql,nope"])
    assert "unknown targets" in capsys.readouterr().err


def test_each_scan_in_the_image_gets_its_own_zap_home() -> None:
    docker = zap_scan.zap_options("docker", "mcp", "mcp-options.prop")
    assert docker == "-configfile /zap/wrk/mcp-options.prop"
    local = {t: zap_scan.zap_options("local", t) for t in zap_scan.TARGETS}
    assert all(v.startswith("-dir ") for v in local.values())
    assert len(set(local.values())) == len(zap_scan.TARGETS)
