# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
GSI Attribute Backfill Lambda for LMA EventSourcingTable.

Backfills the ItemType attribute on existing `cls#…` list-tracking items so they
populate the TypeDateIndex GSI. New items written by the createCall mutation
already include ItemType, so this custom resource is only needed on first
deployment of the GSI feature against a pre-existing stack.

Two handlers:
- `lambda_handler`: Worker invoked by the Step Functions Distributed Map (one
  invocation per parallel-scan segment).
- `handler`: CloudFormation Custom Resource trigger that starts the Step
  Functions execution on stack create/update.

PK prefix → ItemType mapping:
- cls#    → ItemType = "call"   (meeting list-tracking rows, go into GSI)
- c#      → skip (call detail item, not needed in GSI)
- ts#, trs#, chat#, mcp#, …      → skip (all non-list rows)
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# PK prefix → ItemType mapping (only list-tracking rows go into the GSI)
PK_PREFIX_TO_ITEM_TYPE = {
    "cls#": "call",
}

# Safety margin before Lambda timeout (60 seconds)
TIMEOUT_SAFETY_MARGIN_MS = 60_000


def lambda_handler(event, context):
    """
    Process one segment of the EventSourcingTable parallel scan.

    Input event:
    {
        "tableName": "stack-EventSourcingTable-xxx",
        "segment": 0,
        "totalSegments": 10,
        "exclusiveStartKey": null | {...},  // for continuation
        "dryRun": false
    }

    Output:
    {
        "segment": 0,
        "processed": 1500,
        "updated": 1200,
        "skipped": 300,
        "errors": 0,
        "continuation": null | {...},
        "complete": true | false
    }
    """
    table_name = event["tableName"]
    segment = event["segment"]
    total_segments = event["totalSegments"]
    exclusive_start_key = event.get("exclusiveStartKey")
    dry_run = event.get("dryRun", False)

    logger.info(
        "Starting backfill: segment=%s/%s, table=%s, continuation=%s, dryRun=%s",
        segment,
        total_segments,
        table_name,
        "yes" if exclusive_start_key else "no",
        dry_run,
    )

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    stats = {
        "segment": segment,
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "continuation": None,
        "complete": False,
    }

    scan_kwargs = {
        "Segment": segment,
        "TotalSegments": total_segments,
        "ProjectionExpression": "PK, SK, #it",
        "ExpressionAttributeNames": {"#it": "ItemType"},
    }

    if exclusive_start_key:
        scan_kwargs["ExclusiveStartKey"] = exclusive_start_key

    while True:
        remaining_ms = context.get_remaining_time_in_millis()
        if remaining_ms < TIMEOUT_SAFETY_MARGIN_MS:
            logger.warning(
                "Approaching timeout (%dms remaining). Returning continuation for segment %s. Stats: %s",
                remaining_ms,
                segment,
                stats,
            )
            stats["continuation"] = scan_kwargs.get("ExclusiveStartKey")
            return stats

        try:
            response = table.scan(**scan_kwargs)
        except ClientError as e:
            logger.error("Scan error on segment %s: %s", segment, e)
            stats["errors"] += 1
            raise

        items = response.get("Items", [])
        logger.info(
            "Segment %s: scanned %d items (total processed so far: %d)",
            segment,
            len(items),
            stats["processed"],
        )

        for item in items:
            stats["processed"] += 1
            pk = item.get("PK", "")

            # Already stamped?  Skip.
            if "ItemType" in item:
                stats["skipped"] += 1
                continue

            # Only stamp list-tracking rows (cls#…)
            new_item_type = None
            for prefix, item_type in PK_PREFIX_TO_ITEM_TYPE.items():
                if pk.startswith(prefix):
                    new_item_type = item_type
                    break
            if not new_item_type:
                stats["skipped"] += 1
                continue

            if dry_run:
                stats["updated"] += 1
                continue

            try:
                table.update_item(
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression="SET #it = :v",
                    ConditionExpression="attribute_not_exists(#it)",
                    ExpressionAttributeNames={"#it": "ItemType"},
                    ExpressionAttributeValues={":v": new_item_type},
                )
                stats["updated"] += 1
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "ConditionalCheckFailedException":
                    # Already updated (race with live traffic) — count as skipped
                    stats["skipped"] += 1
                else:
                    logger.error("Update error for PK=%s: %s", pk, e)
                    stats["errors"] += 1

        last_key = response.get("LastEvaluatedKey")
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key
        else:
            stats["complete"] = True
            logger.info("Segment %s complete. Stats: %s", segment, stats)
            return stats


def _send_cfn_response(event, context, status, data=None, reason=""):
    """Send response to CloudFormation custom resource."""
    import urllib3

    response_url = event.get("ResponseURL", "")
    if not response_url:
        logger.warning("No ResponseURL in event — skipping CFN response")
        return

    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": event.get("LogicalResourceId", context.log_stream_name),
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data or {},
    }

    # Bounded timeout so a hung CFN endpoint can't pin the Lambda until its
    # own timeout expires (would leave the stack in UPDATE_IN_PROGRESS).
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5.0, read=15.0))
    try:
        http.request(
            "PUT",
            response_url,
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": ""},
            retries=urllib3.Retry(total=2, backoff_factor=0.5),
        )
        logger.info("CFN response sent: %s", status)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to send CFN response: %s", e)


def handler(event, context):
    """CloudFormation Custom Resource handler that triggers backfill."""
    logger.info("Custom Resource event: %s", json.dumps(event))

    request_type = event.get("RequestType", "")

    if request_type == "Delete":
        _send_cfn_response(event, context, "SUCCESS", reason="Delete — no action needed")
        return

    # On Create/Update: always start the state machine.  The worker applies
    # `ConditionExpression="attribute_not_exists(ItemType)"` on every update,
    # so repeated runs are cheap no-ops once the backfill is complete, and
    # any new/legacy rows missing ItemType still get stamped.  We never block
    # the stack update — errors are reported as non-blocking SUCCESS so
    # operators can retry manually.
    #
    # (Previous versions tried to short-circuit by scanning for a cls# row
    # with FilterExpression+Limit=1; that combo is unreliable because DDB
    # applies Limit BEFORE the filter, so valid tables often looked empty
    # and the backfill was skipped — leaving the GSI unpopulated and the
    # meeting list empty for pre-existing stacks.)
    try:
        table_name = event["ResourceProperties"]["TrackingTableName"]
        state_machine_arn = event["ResourceProperties"]["BackfillStateMachineArn"]
        total_segments = int(event["ResourceProperties"].get("TotalSegments", "10"))

        sfn = boto3.client("stepfunctions")
        execution_name = f"backfill-{int(time.time())}"
        sfn.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(
                {
                    "tableName": table_name,
                    "totalSegments": total_segments,
                }
            ),
        )

        logger.info("Started backfill state machine: %s", execution_name)
        _send_cfn_response(
            event,
            context,
            "SUCCESS",
            {
                "BackfillStatus": "STARTED",
                "ExecutionName": execution_name,
                "TotalSegments": str(total_segments),
            },
            reason=f"Backfill started: {execution_name}",
        )

    except Exception as e:  # noqa: BLE001
        logger.error("Error in custom resource handler: %s", e)
        # Don't block the stack — backfill can be retried manually
        _send_cfn_response(
            event,
            context,
            "SUCCESS",
            {"BackfillStatus": "ERROR", "Error": str(e)},
            reason=f"Backfill error (non-blocking): {e}",
        )
