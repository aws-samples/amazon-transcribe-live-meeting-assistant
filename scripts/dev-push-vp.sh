#!/usr/bin/env bash
#
# dev-push-vp.sh — fast iteration on the Virtual Participant container.
#
# Builds the VP backend Docker image locally and pushes it straight to the
# stack's existing ECR repo as :latest, skipping the full CloudFormation
# update + CodeBuild pipeline (~10-40 min) that `lma deploy` runs. The next
# VP task you launch picks up the new image because the ECS task definition
# references the :latest tag.
#
# USE THIS FOR: code-only changes under lma-virtual-participant-stack/backend
#               (zoom-login.ts, ai-dom-resolver.ts, zoom.ts, etc.).
# DO NOT USE FOR: template.yaml changes — those need a real stack update.
#
# Usage:
#   scripts/dev-push-vp.sh [STACK_NAME] [REGION]
#
#   STACK_NAME  VP nested stack name. Default: auto-discovered (the single
#               stack matching *VIRTUALPARTICIPANTSTACK*). Pass explicitly
#               if you have more than one LMA deployment in the account.
#   REGION      AWS region. Default: $AWS_REGION, else us-west-2.
#
# Env:
#   AWS_PROFILE   Honored as-is. Defaults to "default" per repo convention.
#   FORCE_FRESH   If "1", terminates the VP cluster's EC2 host(s) after push
#                 so the next launch pulls the new image on a clean instance.
#                 Off by default — a fresh task already pulls :latest, and
#                 EC2 churn costs ~60-90s of capacity-provider scale-up.
#
# Caveats:
#   - Skips SOCI index generation, so cold-start lazy-loading isn't updated
#     (a few seconds slower first pull; not a correctness issue).
#   - A task already mid-meeting keeps the old code. End it and start a fresh
#     VP to test new code.
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_PROFILE
REGION="${2:-${AWS_REGION:-us-west-2}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/lma-virtual-participant-stack/backend"

log() { printf '\033[1;34m[dev-push-vp]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[dev-push-vp] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found on PATH"
command -v aws >/dev/null || die "aws CLI not found on PATH"
[ -f "$BACKEND_DIR/Dockerfile" ] || die "no Dockerfile at $BACKEND_DIR"

# ----------------------------------------------------------------------
# 1. Resolve the VP stack name (explicit arg, else auto-discover).
# ----------------------------------------------------------------------
STACK_NAME="${1:-}"
if [ -z "$STACK_NAME" ]; then
  log "Auto-discovering VP stack (region=$REGION, profile=$AWS_PROFILE)..."
  mapfile -t MATCHES < <(aws cloudformation list-stacks \
    --region "$REGION" \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE IMPORT_COMPLETE \
    --query "StackSummaries[?contains(StackName, 'VIRTUALPARTICIPANTSTACK')].StackName" \
    --output text | tr '\t' '\n' | grep -v '^$' || true)
  [ "${#MATCHES[@]}" -gt 0 ] || die "no *VIRTUALPARTICIPANTSTACK* stack found — pass STACK_NAME explicitly"
  [ "${#MATCHES[@]}" -eq 1 ] || die "multiple VP stacks found; pass one explicitly:\n$(printf '  %s\n' "${MATCHES[@]}")"
  STACK_NAME="${MATCHES[0]}"
fi
log "VP stack: $STACK_NAME"

# ----------------------------------------------------------------------
# 2. Read ECR repo URI + cluster ARN from stack outputs.
# ----------------------------------------------------------------------
get_output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}
ECR_URI="$(get_output ECRRepositoryUri)"
CLUSTER_ARN="$(get_output ClusterArn)"
[ -n "$ECR_URI" ] && [ "$ECR_URI" != "None" ] || die "could not read ECRRepositoryUri output from $STACK_NAME"
REGISTRY="${ECR_URI%%/*}"            # 1234.dkr.ecr.us-west-2.amazonaws.com
log "ECR repo: $ECR_URI"

# ----------------------------------------------------------------------
# 3. Docker login → build → push.
# ----------------------------------------------------------------------
log "Logging in to ECR..."
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

log "Building image (context: $BACKEND_DIR)..."
docker build -t "$ECR_URI:latest" "$BACKEND_DIR"

log "Pushing $ECR_URI:latest ..."
docker push "$ECR_URI:latest"

# ----------------------------------------------------------------------
# 4. Optionally force a fresh EC2 host so the next launch pulls clean.
# ----------------------------------------------------------------------
if [ "${FORCE_FRESH:-0}" = "1" ] && [ -n "$CLUSTER_ARN" ] && [ "$CLUSTER_ARN" != "None" ]; then
  log "FORCE_FRESH=1 — terminating VP cluster EC2 host(s) to force a clean pull..."
  CIS=$(aws ecs list-container-instances --cluster "$CLUSTER_ARN" --region "$REGION" \
        --query 'containerInstanceArns' --output text)
  for CI in $CIS; do
    [ -n "$CI" ] && [ "$CI" != "None" ] || continue
    EC2_ID=$(aws ecs describe-container-instances --cluster "$CLUSTER_ARN" \
      --container-instances "$CI" --region "$REGION" \
      --query 'containerInstances[0].ec2InstanceId' --output text)
    [ -n "$EC2_ID" ] && [ "$EC2_ID" != "None" ] || continue
    ASG=$(aws autoscaling describe-auto-scaling-instances --instance-ids "$EC2_ID" \
      --region "$REGION" --query 'AutoScalingInstances[0].AutoScalingGroupName' \
      --output text 2>/dev/null || echo "")
    if [ -n "$ASG" ] && [ "$ASG" != "None" ]; then
      log "  terminating $EC2_ID via ASG $ASG"
      aws autoscaling terminate-instance-in-auto-scaling-group --instance-id "$EC2_ID" \
        --no-should-decrement-desired-capacity --region "$REGION" >/dev/null
    else
      log "  terminating $EC2_ID directly"
      aws ec2 terminate-instances --instance-ids "$EC2_ID" --region "$REGION" >/dev/null
    fi
  done
fi

log "Done. Launch a new Virtual Participant to run the updated image."
[ "${FORCE_FRESH:-0}" = "1" ] || log "(A freshly launched task pulls :latest automatically. Set FORCE_FRESH=1 to recycle the EC2 host instead.)"
