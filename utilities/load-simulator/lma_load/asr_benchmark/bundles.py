# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Which ASR bundle a stack runs, and switching it by updating the stack's own template."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

MAIN_BUNDLE_RE = re.compile(
    r"^(?P<indent> {6}ModelBundle: *)(?P<bundle>\S+)(?P<tail> *)$", re.MULTILINE
)
TEMPLATE_URL_RE = re.compile(r"TemplateURL:\s*['\"]?(https://[^\s'\"]+)")
ASR_STACK_LOGICAL_ID = "ASRMICROVMSTACK"
CATALOG_RELATIVE = Path("lma-asr-microvm-stack") / "source" / "catalog.json"


def find_catalog(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / CATALOG_RELATIVE
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"{CATALOG_RELATIVE} not found above {here}; run from an LMA checkout or pass --catalog"
    )


def load_catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    data = json.loads((path or find_catalog()).read_text())
    return {entry["id"]: entry for entry in data.get("bundles", [])}


def validate_bundles(requested: list[str], catalog: dict[str, dict[str, Any]]) -> None:
    unknown = [b for b in requested if b not in catalog]
    if unknown:
        raise ValueError(f"unknown bundle(s) {unknown}; known: {sorted(catalog)}")


def rewrite_bundle(template: str, bundle: str) -> str:
    matches = MAIN_BUNDLE_RE.findall(template)
    if len(matches) != 1:
        raise ValueError(
            f"expected one AsrDefaults ModelBundle line in the template, found {len(matches)}"
        )
    return MAIN_BUNDLE_RE.sub(lambda m: f"{m.group('indent')}{bundle}{m.group('tail')}", template)


def template_bucket(template: str) -> str | None:
    for url in TEMPLATE_URL_RE.findall(template):
        parsed = urlparse(url)
        host = parsed.netloc
        if host.startswith(("s3.", "s3-")):
            return parsed.path.lstrip("/").split("/", 1)[0]
        if ".s3." in host or ".s3-" in host:
            return host.split(".s3", 1)[0]
    return None


def asr_stack_name(cfn, stack_name: str) -> str:
    resource = cfn.describe_stack_resource(
        StackName=stack_name, LogicalResourceId=ASR_STACK_LOGICAL_ID
    )
    return resource["StackResourceDetail"]["PhysicalResourceId"]


def current_bundle(cfn, stack_name: str) -> str:
    nested = asr_stack_name(cfn, stack_name)
    outputs = cfn.describe_stacks(StackName=nested)["Stacks"][0].get("Outputs", [])
    for output in outputs:
        if output["OutputKey"] == "AsrModelBundleId":
            return output["OutputValue"]
    raise RuntimeError(f"{nested} has no AsrModelBundleId output; is EnableMicrovmAsr on?")


def switch_bundle(
    session,
    stack_name: str,
    bundle: str,
    run_id: str,
    bucket: str | None = None,
    wait_minutes: int = 90,
) -> bool:
    cfn = session.client("cloudformation")
    if current_bundle(cfn, stack_name) == bundle:
        logger.info("%s already runs %s", stack_name, bundle)
        return False
    template = cfn.get_template(StackName=stack_name, TemplateStage="Original")["TemplateBody"]
    if not isinstance(template, str):
        raise TypeError("the stack template is JSON; only the published YAML template is supported")
    body = rewrite_bundle(template, bundle)
    bucket = bucket or template_bucket(template)
    if not bucket:
        raise RuntimeError(
            "could not tell which bucket the stack's templates live in; pass --template-bucket"
        )
    key = f"asr-benchmark/{run_id}/lma-main-{bundle}.yaml"
    session.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode())
    region = session.region_name
    url = f"https://s3.{region}.amazonaws.com/{bucket}/{key}"

    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    parameters = [
        {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
        for p in stack.get("Parameters", [])
    ]
    capabilities = stack.get("Capabilities") or ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
    logger.info("Switching %s to %s (template %s)", stack_name, bundle, url)
    try:
        cfn.update_stack(
            StackName=stack_name, TemplateURL=url, Parameters=parameters, Capabilities=capabilities
        )
    except ClientError as err:
        if "No updates are to be performed" in str(err):
            return False
        raise
    cfn.get_waiter("stack_update_complete").wait(
        StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": wait_minutes * 2}
    )
    actual = current_bundle(cfn, stack_name)
    if actual != bundle:
        raise RuntimeError(
            f"stack update finished but the ASR stack reports {actual}, not {bundle}"
        )
    return True
