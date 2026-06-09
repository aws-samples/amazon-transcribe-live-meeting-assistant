#!/usr/bin/env bash
#
# dev-push-vp.sh — fast iteration on the Virtual Participant container.
#
# Builds the VP backend Docker image locally and pushes it straight to the
# stack's existing ECR repo as :latest, skipping the full CloudFormation
# update + CodeBuild pipeline (~10-40 min) that `lma deploy` runs.
#
# IMPORTANT — why we recycle the EC2 host by default:
#   The VP hosts run with ECS_IMAGE_PULL_BEHAVIOR=prefer-cached (see the
#   host userdata in template.yaml). They pre-pull :latest ONCE at boot and
#   every task afterwards reuses that cached image. So pushing a new :latest
#   to ECR is NOT enough — a new task on an already-running host keeps
#   running the stale cached image (silently: same tag, older digest). To
#   actually deliver new code we terminate the host so the ASG launches a
#   fresh one that pre-pulls the new :latest. Set RECYCLE_HOST=0 to skip
#   (e.g. you know the host is fresh, or you're on FARGATE).
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
#   RECYCLE_HOST  Default "1": after push, terminate the VP cluster's EC2
#                 host(s) so the ASG launches a fresh host that pre-pulls the
#                 new :latest. Set "0" to skip (FARGATE, or host known fresh).
#                 NOTE: this will kill any in-progress VP task on that host —
#                 don't run mid-meeting.
#
# Caveats:
#   - Skips SOCI index generation, so cold-start lazy-loading isn't updated
#     (a few seconds slower first pull; not a correctness issue).
#   - Recycling adds ~60-90s (instance launch + ECS register + image pull)
#     before the next VP can start.
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
# Stamp the image so logs/UI can identify exactly what's running. buildDate
# defaults to the in-build `date`; we pass the local git commit (with a -dirty
# suffix if the tree has uncommitted changes) and mark the source as this script.
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
  GIT_COMMIT="${GIT_COMMIT}-dirty"
fi
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Fargate (and the EC2 launch type) run linux/amd64. On Apple Silicon a plain
# `docker build` produces an arm64-only manifest, which Fargate rejects with
# "image Manifest does not contain descriptor matching platform 'linux/amd64'".
# Force the target platform (override with TARGET_PLATFORM if ever needed) and
# use buildx so the build + push emit a single-arch amd64 manifest the task can
# pull. buildx --push builds and pushes in one step (no separate docker push).
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"
log "Build stamp: date=$BUILD_DATE commit=$GIT_COMMIT source=dev-push-vp.sh platform=$TARGET_PLATFORM"
# Build the target-platform image into the LOCAL docker image store (--load),
# then push separately. buildx `--push` with the default `docker` driver was
# observed to hang at the ECR auth/manifest handshake; the build-then-`docker
# push` path is reliable (and emits a plain single-arch manifest Fargate pulls
# cleanly). --provenance=false keeps it a single image, not an OCI index.
log "Building $ECR_URI:latest for $TARGET_PLATFORM (buildx --load)..."
docker buildx build \
  --platform "$TARGET_PLATFORM" \
  --provenance=false \
  --build-arg "BUILD_DATE=$BUILD_DATE" \
  --build-arg "GIT_COMMIT=$GIT_COMMIT" \
  --build-arg "BUILD_SOURCE=dev-push-vp.sh" \
  -t "$ECR_URI:latest" \
  --load \
  "$BACKEND_DIR"

log "Pushing $ECR_URI:latest ..."
docker push "$ECR_URI:latest"

# ----------------------------------------------------------------------
# 4. Recycle the EC2 host(s) so the new :latest is actually pulled.
#    Hosts run ECS_IMAGE_PULL_BEHAVIOR=prefer-cached, so a new task on an
#    existing host would otherwise keep the stale cached image. ON by
#    default; RECYCLE_HOST=0 to skip.
# ----------------------------------------------------------------------
if [ "${RECYCLE_HOST:-1}" = "1" ] && [ -n "$CLUSTER_ARN" ] && [ "$CLUSTER_ARN" != "None" ]; then
  log "Recycling VP cluster EC2 host(s) so the new image is pulled (hosts are prefer-cached)..."
  CIS=$(aws ecs list-container-instances --cluster "$CLUSTER_ARN" --region "$REGION" \
        --query 'containerInstanceArns' --output text)
  if [ -z "$CIS" ] || [ "$CIS" = "None" ]; then
    log "  no container instances registered — the next VP launch boots a fresh host that pulls :latest."
  fi
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
  log "Host recycle requested — wait ~60-90s for the fresh host to register before launching a VP."
fi

if [ "${RECYCLE_HOST:-1}" = "1" ]; then
  log "Done. Wait for the fresh host, then launch a new Virtual Participant."
else
  log "Done (RECYCLE_HOST=0). NOTE: existing prefer-cached hosts will keep the OLD image; only a freshly-booted host pulls the new :latest."
fi
