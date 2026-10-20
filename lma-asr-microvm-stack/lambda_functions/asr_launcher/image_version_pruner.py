# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""CloudFormation custom resource that keeps a MicroVM image under its version cap.

WHY THIS EXISTS
---------------
A MicroVM image holds at most 50 versions, and every rebuild of the same image
adds one. The VP image is rebuilt whenever the source zip changes, so a stack
that is deployed often reaches the cap and the next `CreateMicrovmImageVersion`
fails -- taking the whole nested stack update with it, at the point where the
image is the first thing CloudFormation touches.

So this runs right after the image resource on every create and update, and
removes the versions nobody needs: everything except the version currently
active and the few most recent, which are what a rollback would want.

TWO THINGS THE API MAKES AWKWARD
--------------------------------
Versions form a chain -- each records the version it was built on top of -- so a
version that another one still names as its base cannot be deleted. Deleting a
span therefore has to proceed oldest-first, and even then a delete can come back
as ConflictException while the one before it is still settling, so the passes
repeat until nothing more can go.

And `imageIdentifier` must be the image's full ARN; the bare name is rejected as
a malformed ARN.

Pruning is housekeeping, so it never fails the stack: anything unexpected is
logged and reported as success with a message. The cap is enforced by the image
build, which is upstream of this and will say so plainly if it is reached.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from microvm_client import MicrovmClient, MicrovmError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

# How many non-active versions to leave behind. Enough for a rollback or two,
# small enough that a frequently redeployed stack never approaches the cap.
DEFAULT_KEEP_VERSIONS = 5
# A delete blocked by the chain usually clears once the one before it finishes,
# so retry the whole remaining set rather than giving up on the first refusal.
MAX_PASSES = 6
PASS_PAUSE_SECONDS = 2
# States that mean the version is already on its way out, so leave it alone.
GONE_STATES = frozenset({"DELETING", "DELETED"})


def _list_all_versions(client: MicrovmClient, image_arn: str) -> list[dict]:
    versions: list[dict] = []
    next_token = None
    while True:
        page = client.list_microvm_image_versions(
            image_arn, max_results=100, next_token=next_token
        )
        versions.extend(page.get("items") or [])
        next_token = page.get("nextToken")
        if not next_token:
            return versions


def _versions_to_delete(
    versions: list[dict], active_version: str | None, keep: int
) -> list[str]:
    """Oldest first, which is the only order the base-image chain permits."""
    candidates = [
        version
        for version in versions
        if version.get("state") not in GONE_STATES
        and version.get("imageVersion") != active_version
    ]
    # createdAt is an ISO-8601 string from the REST API, which sorts
    # chronologically as text; fall back to the version itself so a missing
    # timestamp cannot make the order arbitrary.
    candidates.sort(key=lambda v: (v.get("createdAt") or "", v.get("imageVersion") or ""))
    surplus = len(candidates) - max(keep, 0)
    if surplus <= 0:
        return []
    return [version["imageVersion"] for version in candidates[:surplus]]


def prune_microvm_image_versions(
    client: MicrovmClient, image_arn: str, keep: int
) -> dict:
    """Remove all but the active version and the `keep` most recent."""
    image = client.get_microvm_image(image_arn)
    active = image.get("latestActiveImageVersion")
    versions = _list_all_versions(client, image_arn)
    doomed = _versions_to_delete(versions, active, keep)
    logger.info(
        "image %s has %d version(s), active %s; removing %d: %s",
        image_arn,
        len(versions),
        active,
        len(doomed),
        doomed,
    )

    deleted: list[str] = []
    blocked = list(doomed)
    for attempt in range(MAX_PASSES):
        if not blocked:
            break
        if attempt:
            time.sleep(PASS_PAUSE_SECONDS)
        still_blocked: list[str] = []
        for version in blocked:
            try:
                client.delete_microvm_image_version(image_arn, version)
                deleted.append(version)
            except MicrovmError as error:
                if "Conflict" in error.code or error.status == 409:
                    still_blocked.append(version)
                    continue
                # Anything else is not going to be fixed by another pass.
                logger.warning("version %s could not be removed: %s", version, error)
        if len(still_blocked) == len(blocked):
            # A whole pass achieved nothing, so further passes will not either.
            logger.info("no further versions can be removed this run")
            blocked = still_blocked
            break
        blocked = still_blocked

    return {
        "TotalVersions": len(versions),
        "ActiveVersion": active or "",
        "Deleted": len(deleted),
        "Remaining": len(versions) - len(deleted),
    }


def _send_response(event: dict, context, status: str, data: dict, reason: str) -> None:
    """Report to CloudFormation.

    Written out by hand because `cfnresponse` is only injected into inline
    `Code: ZipFile` functions, and this one ships in a deployment package so it
    can import the MicroVM client alongside it.
    """
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason[:1000],
            "PhysicalResourceId": str(
                event.get("PhysicalResourceId")
                or getattr(context, "log_stream_name", None)
                or "microvm-image-pruner"
            ),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "NoEcho": False,
            "Data": {key: str(value) for key, value in data.items()},
        }
    ).encode()
    request = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        logger.info("reported %s to CloudFormation (HTTP %s)", status, response.status)


def lambda_handler(event: dict, context) -> None:
    logger.info("event: %s", json.dumps({k: v for k, v in event.items() if k != "ResponseURL"}))
    properties = event.get("ResourceProperties") or {}
    outcome: dict = {}
    reason = "OK"
    try:
        if event["RequestType"] == "Delete":
            # The image resource is being removed with the stack, and its
            # versions go with it.
            reason = "Nothing to prune on stack delete"
        else:
            image_arn = properties.get("ImageArn") or ""
            if not image_arn:
                raise ValueError("ImageArn is required")
            keep = int(properties.get("KeepVersions") or DEFAULT_KEEP_VERSIONS)
            outcome = prune_microvm_image_versions(
                MicrovmClient(region=os.environ.get("AWS_REGION")), image_arn, keep
            )
            reason = (
                f"Removed {outcome['Deleted']} of {outcome['TotalVersions']} version(s); "
                f"{outcome['Remaining']} remain"
            )
        logger.info(reason)
    except Exception as error:  # noqa: BLE001 - see module docstring
        # Housekeeping does not get to block a deployment.
        reason = f"Pruning was skipped: {error}"
        logger.warning(reason, exc_info=True)
    try:
        _send_response(event, context, "SUCCESS", outcome, reason)
    except (urllib.error.URLError, KeyError) as error:
        logger.error("could not report to CloudFormation: %s", error)
        raise
