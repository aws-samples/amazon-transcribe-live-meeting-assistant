# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""LMA end-to-end integration tests against a live deployed stack.

Run with ``make integ-tests STACK=<name>`` (see the ``integ-tests`` skill).

These validate the parts of the dependency-upgrade batch (PR #400) that unit
tests and static analysis cannot reach:

  * Fastify 3->5 + @fastify/websocket 5->11 on the internet-facing transcriber
    (its ECS service must be healthy and the /health/check route must answer).
  * AppSync GraphQL reachability (the UI's whole real-time data plane).
  * The Virtual Participant launch path, which for platform=ZOOM exercises the
    @zoom/meetingsdk 4->6 code path end to end.

Test tiers:
  * Default run: read-only + a self-cleaning VP *registry* lifecycle (no real
    meeting). Safe to repeat against any stack.
  * Opt-in live join: pass --vp-meeting-id (a real Zoom meeting) to actually
    place the VP into a meeting and assert it leaves INITIALIZING.
"""

from __future__ import annotations

import pytest

from lma_sdk import LMAClient

# ── Expected stack outputs (a subset that must always be present) ──────────
REQUIRED_OUTPUTS = [
    "LMAWebsocketEndpoint",
    "ApplicationCloudfrontEndpoint",
    "CallDataStreamName",
    "CognitoUserPoolClientId",
]


# ────────────────────────────────────────────────────────────────────────────
# 1. Stack shape
# ────────────────────────────────────────────────────────────────────────────

def test_stack_status_is_complete(client: LMAClient) -> None:
    """The stack exists and is in a COMPLETE (not failed/in-progress) state."""
    result = client.stack.status()
    assert result.exists and result.stack is not None, (
        f"stack {client.stack_name!r} not found"
    )
    state = result.stack.status
    assert state.endswith("_COMPLETE"), (
        f"stack {client.stack_name!r} status is {state!r}, expected a *_COMPLETE state"
    )
    assert "ROLLBACK" not in state, f"stack is in a rollback state: {state!r}"


@pytest.mark.parametrize("key", REQUIRED_OUTPUTS)
def test_required_output_present(outputs: dict[str, str], key: str) -> None:
    """Each output the UI / extensions depend on is present and non-empty."""
    assert key in outputs and outputs[key].strip(), (
        f"required stack output {key!r} missing or empty"
    )


# ────────────────────────────────────────────────────────────────────────────
# 2. WebSocket transcriber — Fastify 5 (internet-facing runtime)
# ────────────────────────────────────────────────────────────────────────────

def test_transcriber_alb_targets_healthy(client: LMAClient) -> None:
    """The transcriber ALB target group reports at least one HEALTHY target.

    ``/health/check`` is the ALB target-group health path and is intentionally
    NOT routed through the public CloudFront distribution (which only forwards
    ``/api/v1/ws``), so it can't be probed over the internet. The authoritative
    signal that the Fastify 5 server booted and is serving that route is the ALB
    marking its ECS target HEALTHY — a boot failure or crash loop leaves targets
    ``unhealthy``/``draining`` or the group empty. We locate the transcriber
    target group by its CloudFormation stack-name tag.
    """
    elbv2 = client.session.client("elbv2", region_name=client.region)
    stack_lc = client.stack_name.lower()

    matched_arn = None
    paginator = elbv2.get_paginator("describe_target_groups")
    for page in paginator.paginate():
        for tg in page.get("TargetGroups", []):
            if tg.get("HealthCheckPath") != "/health/check":
                continue
            arn = tg["TargetGroupArn"]
            tags = elbv2.describe_tags(ResourceArns=[arn])["TagDescriptions"][0]["Tags"]
            stack = next((t["Value"] for t in tags if t["Key"] == "aws:cloudformation:stack-name"), "")
            if stack_lc in stack.lower():
                matched_arn = arn
                break
        if matched_arn:
            break

    if not matched_arn:
        pytest.skip("no transcriber target group (/health/check) found for this stack")

    health = elbv2.describe_target_health(TargetGroupArn=matched_arn)
    states = [d["TargetHealth"]["State"] for d in health.get("TargetHealthDescriptions", [])]
    assert states, "transcriber target group has no registered targets (ECS task not attached)"
    assert "healthy" in states, (
        f"transcriber ALB targets not healthy: {states}. The Fastify 5 server "
        f"may have failed to boot or is failing /health/check."
    )


def test_transcriber_ecs_service_running(client: LMAClient, outputs: dict[str, str]) -> None:
    """The transcriber ECS service has running tasks and none are crash-looping.

    Directly inspects ECS so a task that boots, 500s, and restarts (which a
    Fastify misconfig would cause) is caught even if a health probe happens to
    hit a briefly-healthy task.
    """
    ecs = client.session.client("ecs", region_name=client.region)
    clusters = ecs.list_clusters().get("clusterArns", [])
    lma_clusters = [c for c in clusters if client.stack_name.lower() in c.lower()]
    # Fall back to scanning all clusters for a transcriber service if the
    # cluster isn't name-stamped with the stack name.
    search = lma_clusters or clusters
    found_service = False
    for cluster in search:
        svc_arns = ecs.list_services(cluster=cluster, maxResults=100).get("serviceArns", [])
        if not svc_arns:
            continue
        for i in range(0, len(svc_arns), 10):
            desc = ecs.describe_services(cluster=cluster, services=svc_arns[i:i + 10])
            for svc in desc.get("services", []):
                name = svc.get("serviceName", "")
                if "transcrib" not in name.lower() and "websocket" not in name.lower():
                    continue
                found_service = True
                assert svc["runningCount"] >= 1, (
                    f"transcriber service {name} has runningCount="
                    f"{svc['runningCount']} (expected >=1)"
                )
                assert svc["runningCount"] >= svc["desiredCount"], (
                    f"transcriber service {name}: running "
                    f"{svc['runningCount']} < desired {svc['desiredCount']} "
                    f"(possible crash loop)"
                )
    if not found_service:
        pytest.skip("no transcriber/websocket ECS service found to inspect")


# ────────────────────────────────────────────────────────────────────────────
# 3. AppSync GraphQL data plane
# ────────────────────────────────────────────────────────────────────────────

def test_appsync_reachable(client: LMAClient) -> None:
    """The AppSync GraphQL API is resolvable and answers an IAM-signed query.

    Uses listVirtualParticipants (an @aws_iam field) as a lightweight probe —
    proves URL resolution, SigV4 auth, and resolver execution all work.
    """
    rows = client.vp.list()
    assert isinstance(rows, list), "listVirtualParticipants should return a list"


# ────────────────────────────────────────────────────────────────────────────
# 4. Virtual Participant lifecycle (Zoom SDK 6 code path)
# ────────────────────────────────────────────────────────────────────────────

def test_vp_registry_lifecycle(client: LMAClient, request: pytest.FixtureRequest) -> None:
    """Create a VP registry row without launching, confirm it lists, then end it.

    ``wait=False`` skips the Step Functions launch poll, so this stays fast and
    doesn't require a real meeting — it validates the AppSync CRUD surface the
    UI uses. Always cleans up the row it creates.
    """
    platform = request.config.getoption("--vp-platform")
    created = client.vp.create(
        meeting_name="integ-test (registry only)",
        platform=platform,
        meeting_id="0000000000",
        user_name="lma-integ-test",
        wait=False,
    )
    vp_id = created.id
    try:
        assert vp_id, "create should return a VP id"
        fetched = client.vp.get(vp_id)
        assert fetched.id == vp_id
        ids = {r.id for r in client.vp.list()}
        assert vp_id in ids, "created VP should appear in list()"
    finally:
        ended = client.vp.end(vp_id, reason="integ-test cleanup")
        assert ended.id == vp_id


@pytest.mark.live
def test_vp_live_join(client: LMAClient, request: pytest.FixtureRequest) -> None:
    """OPT-IN: actually place the VP into a real meeting (Zoom SDK 6 path).

    Skipped unless --vp-meeting-id is provided. When platform is ZOOM this
    exercises @zoom/meetingsdk 6 end to end: the VP must leave INITIALIZING
    (i.e. the ECS VP task launched, loaded the SDK, and reported status).
    """
    meeting_id = request.config.getoption("--vp-meeting-id")
    if not meeting_id:
        pytest.skip("no --vp-meeting-id provided; skipping live VP join test")
    platform = request.config.getoption("--vp-platform")
    password = request.config.getoption("--vp-meeting-password")

    result = client.vp.create(
        meeting_name="integ-test live join",
        platform=platform,
        meeting_id=meeting_id,
        meeting_password=password,
        user_name="LMA Integ Test",
        wait=True,
        timeout_s=180.0,
    )
    try:
        assert result.status != "INITIALIZING", (
            f"VP never left INITIALIZING (status={result.status}); the "
            f"{platform} join path (SDK) may have failed to launch"
        )
        assert result.status != "FAILED", (
            f"VP launch FAILED — check the VP ECS task logs. sfn_status="
            f"{result.sfn_status}"
        )
    finally:
        client.vp.end(result.id, reason="integ-test live cleanup")
