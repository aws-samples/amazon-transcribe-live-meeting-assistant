# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Which ASR bundle a stack runs, and switching it by updating the stack's own template."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

NESTED_STACK_TYPE = "AWS::CloudFormation::Stack"
RESOURCE_HEADING_RE = re.compile(r"^  ([A-Za-z0-9]+):\s*$")
NESTED_URL_RE = re.compile(r"^\s+TemplateURL:\s*(https://\S+)\s*$")
TEMPLATE_PREFIX = "asr-benchmark"


class StaleTemplatesError(RuntimeError):
    """The stack's published nested templates differ from the ones it runs."""


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


def wait_until_stable(cfn, stack_name: str, timeout_s: int = 5400, poll_s: int = 30) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        if not status.endswith("_IN_PROGRESS"):
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{stack_name} is still {status} after {timeout_s // 60} minutes")
        logger.info("%s is %s; waiting before the bundle switch", stack_name, status)
        time.sleep(poll_s)


def nested_template_urls(template: str) -> dict[str, str]:
    urls, current = {}, None
    for line in template.splitlines():
        heading = RESOURCE_HEADING_RE.match(line)
        if heading:
            current = heading.group(1)
            continue
        found = NESTED_URL_RE.match(line)
        if found and current:
            urls[current] = found.group(1)
    return urls


def _s3_location(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host, path = parsed.netloc, parsed.path.lstrip("/")
    if host.startswith(("s3.", "s3-")):
        bucket, _, key = path.partition("/")
        return bucket, key
    return host.split(".s3", 1)[0], path


def _comparable(text: str) -> str:
    return "".join(ch if ord(ch) < 128 else "?" for ch in text).strip()


def stale_nested_stacks(session, stack_name: str, template: str) -> list[str]:
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    physical = {
        r["LogicalResourceId"]: r["PhysicalResourceId"]
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name)
        for r in page["StackResourceSummaries"]
        if r["ResourceType"] == NESTED_STACK_TYPE and r.get("PhysicalResourceId")
    }
    stale = []
    for logical, url in sorted(nested_template_urls(template).items()):
        if logical not in physical:
            continue
        deployed = cfn.get_template(StackName=physical[logical], TemplateStage="Original")[
            "TemplateBody"
        ]
        if not isinstance(deployed, str):
            deployed = json.dumps(deployed)
        bucket, key = _s3_location(url)
        try:
            published = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        except ClientError:
            stale.append(f"{logical} (published template missing)")
            continue
        if _comparable(published) != _comparable(deployed):
            stale.append(logical)
    return stale


def template_url(s3, bucket: str, key: str) -> str:
    return f"{s3.meta.endpoint_url.rstrip('/')}/{bucket}/{key}"


def delete_run_templates(session, bucket: str, run_id: str) -> int:
    s3 = session.client("s3")
    prefix = f"{TEMPLATE_PREFIX}/{run_id}/"
    keys = [
        {"Key": o["Key"]}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for o in page.get("Contents", [])
    ]
    if keys:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
    return len(keys)


def switch_bundle(
    session,
    stack_name: str,
    bundle: str,
    run_id: str,
    bucket: str | None = None,
    wait_minutes: int = 90,
) -> bool:
    cfn = session.client("cloudformation")
    wait_until_stable(cfn, stack_name, timeout_s=wait_minutes * 60)
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
    s3 = session.client("s3")
    key = f"{TEMPLATE_PREFIX}/{run_id}/lma-main-{bundle}.yaml"
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode())
    url = template_url(s3, bucket, key)

    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    parameters = [
        {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
        for p in stack.get("Parameters", [])
    ]
    capabilities = stack.get("Capabilities") or ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
    stale = stale_nested_stacks(session, stack_name, template)
    if stale:
        raise StaleTemplatesError(
            f"the published templates of {', '.join(stale)} differ from what {stack_name} runs "
            "(publish.sh was re-run since the last deploy), so a bundle switch would also deploy "
            "them. Deploy that build first, or publish the deployed version again."
        )
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
