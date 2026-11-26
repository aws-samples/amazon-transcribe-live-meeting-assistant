# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Publishing uses the target Region, whatever the environment's default Region is.

The situation these tests set up is ``lma deploy --from-code . --region
us-east-1`` run with a profile and environment that default to us-west-2: the
client's session, AWS_REGION and AWS_DEFAULT_REGION all name us-west-2, and
PublishConfig.region names us-east-1.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from lma_sdk import LMAClient
from lma_sdk._core.publish import Publisher
from lma_sdk.models.publish import PublishConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET_REGION = "us-east-1"
ENV_REGION = "us-west-2"
BUCKET_BASENAME = "lma-artifacts"
BUCKET = f"{BUCKET_BASENAME}-{TARGET_REGION}"

# One stack of each package type that runs a subprocess.
STACKS = [
    "lma-vpc-stack",  # aws cloudformation package
    "lma-meetingassist-setup-stack",  # the stack's own publish.sh
    "lma-ai-stack",  # build-s3-dist.sh -> make release -> sam package
    "lma-llm-template-setup-stack",  # aws cloudformation package + source hash
]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "VERSION").write_text("1.2.3\n")
    (project / "lma-main.yaml").write_text("Resources: {}\n")
    (project / "lma-vpc-stack").mkdir()
    (project / "lma-vpc-stack" / "template.yaml").write_text("Resources: {}\n")
    (project / "lma-meetingassist-setup-stack").mkdir()
    (project / "lma-meetingassist-setup-stack" / "publish.sh").write_text("#!/bin/sh\n")
    (project / "lma-ai-stack" / "deployment").mkdir(parents=True)
    (project / "lma-ai-stack" / "deployment" / "build-s3-dist.sh").write_text("#!/bin/sh\n")
    llm = project / "lma-llm-template-setup-stack"
    (llm / "deployment").mkdir(parents=True)
    (llm / "source").mkdir()
    (llm / "source" / "seed.py").write_text("x = 1\n")
    (llm / "deployment" / "llm-template-setup.yaml").write_text(
        "Resources:\n  Seed:\n    Properties:\n      source_hash: x\n"
    )
    return project


def _child_region(env: dict) -> str:
    """The Region a child AWS CLI resolves: AWS_REGION, AWS_DEFAULT_REGION, then the profile."""
    return env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or ENV_REGION


def _sam_region(env: dict) -> str:
    """The Region SAM CLI (botocore) resolves without --region: AWS_DEFAULT_REGION, then the profile."""
    return env.get("AWS_DEFAULT_REGION") or ENV_REGION


@pytest.fixture
def published(tmp_path, monkeypatch):
    """Run Publisher.publish() with every subprocess and AWS client faked."""
    monkeypatch.setenv("AWS_REGION", ENV_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", ENV_REGION)

    clients: list[tuple[str, str | None]] = []
    validated: list[str] = []

    def make_client(service, region_name=None, **_kw):
        clients.append((service, region_name))
        svc = MagicMock()
        svc.validate_template.side_effect = lambda TemplateURL: validated.append(TemplateURL)
        return svc

    session = MagicMock()
    session.region_name = ENV_REGION
    session.client.side_effect = make_client
    client = LMAClient(stack_name="LMA", session=session)
    assert client.region == ENV_REGION

    children: list[dict] = []
    nested_template_urls: list[str] = []

    def fake_run(cmd, cwd=None, env=None, **_kw):
        if cmd[0] == "git":  # the untracked-files preflight; the project is not a repository
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a git repository")
        children.append({"cmd": cmd, "region": _child_region(env)})
        if "--output-template-file" in cmd:
            Path(cmd[cmd.index("--output-template-file") + 1]).write_text(
                (Path(cwd) / cmd[cmd.index("--template-file") + 1]).read_text()
            )
        if cmd[0].endswith("build-s3-dist.sh"):
            # build-s3-dist.sh <bucket basename> <s3 prefix> <version> <region>.
            # `sam package` writes each nested AWS::CloudFormation::Stack's
            # TemplateURL with the Region it resolves.
            bucket = f"{cmd[1]}-{cmd[4]}"
            nested_template_urls.append(
                f"https://s3.{_sam_region(env)}.amazonaws.com/{bucket}/{cmd[2]}/nested.yaml"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with (
        patch("lma_sdk._core.publish.check_prerequisites", return_value=[]),
        patch("lma_sdk._core.publish.subprocess.run", side_effect=fake_run),
    ):
        result = Publisher(client).publish(
            PublishConfig(
                bucket_basename=BUCKET_BASENAME,
                prefix="lma",
                region=TARGET_REGION,
                project_dir=str(_project(tmp_path)),
                stacks=STACKS,
                force=True,
            )
        )
    return {
        "result": result,
        "clients": clients,
        "validated": validated,
        "children": children,
        "nested_template_urls": nested_template_urls,
    }


def test_every_packaged_template_url_names_the_target_region(published):
    result = published["result"]
    assert result.success, [(r.stack_name, r.message) for r in result.stack_results]
    urls = (
        [result.template_url]
        + [r.s3_template_url for r in result.stack_results if r.s3_template_url]
        + published["validated"]
        + published["nested_template_urls"]
    )
    assert published["nested_template_urls"]
    assert len(urls) >= 5
    for url in urls:
        assert url.startswith(f"https://s3.{TARGET_REGION}.amazonaws.com/{BUCKET}/"), url
    assert result.region == TARGET_REGION
    assert result.bucket == BUCKET


def test_every_client_is_created_in_the_target_region(published):
    services = {service for service, _ in published["clients"]}
    assert {"s3", "cloudformation"} <= services
    assert {region for _, region in published["clients"]} == {TARGET_REGION}


def test_every_packaging_subprocess_runs_in_the_target_region(published):
    children = published["children"]
    assert len(children) == len(STACKS)
    for child in children:
        assert child["region"] == TARGET_REGION, child["cmd"]
        if child["cmd"][:3] == ["aws", "cloudformation", "package"]:
            assert child["cmd"][child["cmd"].index("--region") + 1] == TARGET_REGION
        else:
            # The scripts take the Region as their last argument.
            assert child["cmd"][-1] == TARGET_REGION


@patch("lma_sdk._core.publish.subprocess.run")
def test_run_command_sets_both_region_variables_for_the_child(mock_run, monkeypatch):
    monkeypatch.setenv("AWS_REGION", ENV_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", ENV_REGION)
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    Publisher._run_command(["true"], region=TARGET_REGION)

    env = mock_run.call_args.kwargs["env"]
    assert env["AWS_REGION"] == TARGET_REGION
    assert env["AWS_DEFAULT_REGION"] == TARGET_REGION


@patch("lma_sdk._core.publish.subprocess.run")
def test_run_command_without_a_region_inherits_the_environment(mock_run, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", ENV_REGION)
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    Publisher._run_command(["true"])

    assert mock_run.call_args.kwargs["env"]["AWS_DEFAULT_REGION"] == ENV_REGION


# ── The scripts the publisher runs ────────────────────────────
#
# SAM CLI resolves its Region from AWS_DEFAULT_REGION or the profile, not from
# AWS_REGION, and writes that Region into each nested TemplateURL it packages,
# so the build scripts pass the Region to it explicitly.


def _commands(text: str, tool: str) -> list[str]:
    """Each ``<tool> ...`` command in a shell script or Makefile, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line.strip() for line in joined.splitlines() if line.strip().startswith(tool)]


@pytest.mark.parametrize(
    "path, region_ref",
    [
        ("lma-ai-stack/Makefile", "$(AWS_REGION)"),
        ("lma-websocket-transcriber-stack/deployment/build-s3-dist.sh", "${AWS_REGION}"),
    ],
)
def test_sam_package_and_s3_uploads_name_the_region(path, region_ref):
    text = (REPO_ROOT / path).read_text()
    sam_package = _commands(text, "sam package")
    assert sam_package
    for command in sam_package + _commands(text, "aws s3 cp"):
        assert re.search(r"--region '?" + re.escape(region_ref), command), command


@pytest.mark.parametrize(
    "path",
    [
        "lma-ai-stack/deployment/build-s3-dist.sh",
        "lma-websocket-transcriber-stack/deployment/build-s3-dist.sh",
    ],
)
def test_build_scripts_export_the_region_to_both_variables(path):
    text = (REPO_ROOT / path).read_text()
    assert 'export AWS_DEFAULT_REGION="$AWS_REGION"' in text


@pytest.mark.parametrize(
    "path",
    [
        "lma-meetingassist-setup-stack/publish.sh",
        "lma-bedrockkb-stack/publish.sh",
        "lma-bedrockagent-stack/publish.sh",
    ],
)
def test_stack_publish_scripts_export_both_region_variables(path):
    text = (REPO_ROOT / path).read_text()
    assert "export AWS_DEFAULT_REGION=$REGION" in text
    assert "export AWS_REGION=$REGION" in text
