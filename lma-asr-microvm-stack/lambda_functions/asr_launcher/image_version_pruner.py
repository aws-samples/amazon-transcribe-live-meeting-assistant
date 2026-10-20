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

So this runs right after the image resource and removes the versions nobody
needs: everything except the version currently active and the few most recent,
which are what a rollback would want.

It is triggered by the image's active version, which is what changes when a
rebuild adds one. Keying it on the source location instead would have missed
the rebuilds a property change causes -- a hook timeout, a memory floor, an
environment variable -- none of which touch the source zip.

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
# ListMicrovmImageVersions rejects a maxResults above this ("Member must have
# value less than or equal to 50"), and an image holds at most 50 versions, so
# this fetches everything a page at a time with no clamping on our side.
MAX_RESULTS_PER_PAGE = 50
# The service returns a trailing nextToken with an empty final page, so one
# extra call is normal. The cap is only there so that a token which never
# cleared could not spin until the function timed out.
MAX_PAGES = 10
# A delete refused because another version still names it as a base usually
# clears once that one finishes, so the refused set is retried. A pass that
# achieves nothing does NOT mean later passes will: the refusals are transient,
# which is the whole reason for retrying, so the passes run out rather than
# stopping at the first unproductive one.
MAX_PASSES = 6
PASS_PAUSE_SECONDS = 2
# Stop and report while there is still time to answer CloudFormation. A custom
# resource that never responds leaves the stack waiting an hour and then fails
# it, which is worse than leaving versions behind.
MIN_REMAINING_MS = 30_000
# States that mean the version is already on its way out, so leave it alone.
GONE_STATES = frozenset({"DELETING", "DELETED"})


def _remaining_ms(context) -> float:
    """Milliseconds left, or an effectively unlimited budget if unknown."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return float("inf")
    try:
        return float(getter())
    except (TypeError, ValueError):
        return float("inf")


def _list_all_versions(client: MicrovmClient, image_arn: str) -> list[dict]:
    versions: list[dict] = []
    next_token = None
    for _page in range(MAX_PAGES):
        page = client.list_microvm_image_versions(
            image_arn, max_results=MAX_RESULTS_PER_PAGE, next_token=next_token
        )
        versions.extend(page.get("items") or [])
        token = page.get("nextToken")
        if not token or token == next_token:
            return versions
        next_token = token
    logger.warning("stopped paging after %d pages", MAX_PAGES)
    return versions


def _age_key(version: dict) -> tuple:
    """Sort key: oldest first, on a timestamp of unknown shape.

    `createdAt` is a timestamp, which this client sees as the epoch number
    restJson1 puts on the wire rather than as a string. Comparing a number
    against a string raises, so the shape is normalised here instead of being
    assumed: anything that converts to a float sorts by value, anything else
    sorts by its text, and the two groups are kept apart so the comparison is
    always between like types. The version is the tiebreak, so a missing
    timestamp cannot make the order arbitrary.
    """
    created = version.get("createdAt")
    try:
        return (0, float(created), "", str(version.get("imageVersion") or ""))
    except (TypeError, ValueError):
        return (1, 0.0, str(created or ""), str(version.get("imageVersion") or ""))


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
    candidates.sort(key=_age_key)
    # The active version is excluded above, so it is already safe. If the image
    # reported no active version, though, nothing has been held back yet, and
    # an image with no versions left cannot be launched at all -- so in that
    # case keep at least the newest rather than trusting a blank answer.
    floor = 0 if active_version else 1
    surplus = len(candidates) - max(keep, floor)
    if surplus <= 0:
        return []
    return [version["imageVersion"] for version in candidates[:surplus]]


def prune_microvm_image_versions(
    client: MicrovmClient, image_arn: str, keep: int, context=None
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
    out_of_time = False
    for attempt in range(MAX_PASSES):
        if not blocked or out_of_time:
            break
        if attempt:
            time.sleep(PASS_PAUSE_SECONDS)
        still_blocked: list[str] = []
        for version in blocked:
            if _remaining_ms(context) < MIN_REMAINING_MS:
                out_of_time = True
                still_blocked.append(version)
                continue
            try:
                client.delete_microvm_image_version(image_arn, version)
                deleted.append(version)
            except MicrovmError as error:
                if error.status == 409 or "Conflict" in error.code:
                    still_blocked.append(version)
                    continue
                # Anything else is not going to be fixed by another pass.
                logger.warning("version %s could not be removed: %s", version, error)
        blocked = still_blocked
    if out_of_time:
        logger.warning(
            "stopped with %d version(s) left so there is time to report", len(blocked)
        )
    elif blocked:
        logger.info("%d version(s) still in use as a base; next run will retry", len(blocked))

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
    """Prune on create and update; report success whatever happens."""
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
                MicrovmClient(region=os.environ.get("AWS_REGION")),
                image_arn,
                keep,
                context,
            )
            reason = (
                f"Removed {outcome['Deleted']} of {outcome['TotalVersions']} version(s); "
                f"{outcome['Remaining']} remain"
            )
        logger.info(reason)
    except Exception as error:  # pylint: disable=broad-exception-caught
        # Housekeeping does not get to block a deployment.
        reason = f"Pruning was skipped: {error}"
        logger.warning(reason, exc_info=True)
    try:
        _send_response(event, context, "SUCCESS", outcome, reason)
    except (urllib.error.URLError, KeyError) as error:
        logger.error("could not report to CloudFormation: %s", error)
        raise
