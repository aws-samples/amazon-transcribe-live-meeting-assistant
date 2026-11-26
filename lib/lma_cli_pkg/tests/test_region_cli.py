# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""A --region given on the command line decides the Region, whatever the environment's default is."""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from lma_cli.cli import main

TARGET_REGION = "us-east-1"
ENV_REGION = "us-west-2"


@pytest.fixture
def env_region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", ENV_REGION)
    monkeypatch.setenv("AWS_REGION", ENV_REGION)


def _client():
    client = MagicMock()
    client.stack_name = "LMA"
    client.stack.check_in_progress.return_value = None
    client.stack.exists.return_value = True
    client.publish.publish.return_value = MagicMock(
        success=True,
        template_url=f"https://s3.{TARGET_REGION}.amazonaws.com/lma-123-{TARGET_REGION}/lma/lma-main.yaml",
        stack_results=[],
    )
    return client


def _invoke(args, tmp_path):
    client = _client()
    factory_regions = []

    def factory(**kw):
        factory_regions.append(kw.get("region"))
        return client

    (tmp_path / "lma-main.yaml").write_text("Resources: {}\n")
    result = CliRunner().invoke(main, args, obj={"client_factory": factory})
    return result, client, factory_regions


@pytest.mark.parametrize(
    "args",
    [
        [
            "deploy",
            "--from-code",
            "{src}",
            "--region",
            TARGET_REGION,
            "--bucket-basename",
            "lma-123",
        ],
        [
            "--region",
            TARGET_REGION,
            "deploy",
            "--from-code",
            "{src}",
            "--bucket-basename",
            "lma-123",
        ],
    ],
    ids=["subcommand-flag", "group-flag"],
)
def test_deploy_from_code_builds_and_deploys_in_the_command_line_region(env_region, tmp_path, args):
    args = [a.replace("{src}", str(tmp_path)) for a in args]
    result, client, factory_regions = _invoke(args, tmp_path)

    assert client.publish.publish.called, result.output
    assert client.publish.publish.call_args.kwargs["region"] == TARGET_REGION
    assert factory_regions
    assert set(factory_regions) == {TARGET_REGION}
    assert client.stack.deploy.called, result.output
    template_url = client.stack.deploy.call_args.kwargs["template_url"]
    assert template_url.startswith(f"https://s3.{TARGET_REGION}.amazonaws.com/"), template_url


@pytest.mark.parametrize(
    "args",
    [
        [
            "publish",
            "--source-dir",
            "{src}",
            "--region",
            TARGET_REGION,
            "--bucket-basename",
            "lma-123",
        ],
        [
            "--region",
            TARGET_REGION,
            "publish",
            "--source-dir",
            "{src}",
            "--bucket-basename",
            "lma-123",
        ],
    ],
    ids=["subcommand-flag", "group-flag"],
)
def test_publish_uses_the_command_line_region(env_region, tmp_path, args):
    args = [a.replace("{src}", str(tmp_path)) for a in args]
    _result, client, factory_regions = _invoke(args, tmp_path)

    assert client.publish.publish.call_args.kwargs["region"] == TARGET_REGION
    assert set(factory_regions) == {TARGET_REGION}


def test_without_a_region_flag_the_environment_region_is_used(env_region, tmp_path):
    args = ["publish", "--source-dir", str(tmp_path), "--bucket-basename", "lma-123"]
    _result, client, factory_regions = _invoke(args, tmp_path)

    assert client.publish.publish.call_args.kwargs["region"] == ENV_REGION
    assert set(factory_regions) == {ENV_REGION}


def test_default_client_factory_follows_the_group_region_flag(env_region, monkeypatch):
    """Commands that only create a client (status, outputs, ...) also honour ``lma --region``."""
    created = []

    def fake_make_client(**kw):
        created.append(kw.get("region"))
        client = MagicMock()
        client.stack.status.side_effect = RuntimeError("stop here")
        return client

    monkeypatch.setattr("lma_cli.cli._make_client", fake_make_client)
    CliRunner().invoke(main, ["--region", TARGET_REGION, "status"])

    assert created == [TARGET_REGION]
