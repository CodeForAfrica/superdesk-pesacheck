#!/usr/bin/env bash
# Dump the Superdesk content-config collections from a running stack into a
# mongodump tarball, ready to feed to convert.py.
#
# Part of the "content config as tracked JSON" migration
# (docs/plans/content-config-as-tracked-json.md). Pairs with convert.py: this
# captures the current state of a running instance, convert.py turns it into the
# tracked tree, and the diff is the review. Read-only against MongoDB; writes
# only ephemeral files (under /tmp on the host and inside the container) which it
# cleans up.
#
# Usage:
#   ./dump.sh local  [OUT.tgz]                 # local Compose stack (docker exec)
#   ./dump.sh staging [OUT.tgz]                # deployed stack over SSM
#   ./dump.sh prod    [OUT.tgz]                # deployed stack over SSM
#
# Env overrides:
#   REGION  (default eu-west-1)                deployed only
#   DB      (default superdesk)
#   LOCAL_MONGO_CONTAINER                      auto-detected if unset (local only)
#
# The collections dumped are exactly bootstrap_superdesk.py's CONFIG_COLLECTIONS.
set -euo pipefail

SOURCE="${1:-}"
OUT="${2:-./superdesk-content-config.${SOURCE}.tgz}"
REGION="${REGION:-eu-west-1}"
DB="${DB:-superdesk}"
COLLECTIONS="content_types content_templates vocabularies content_filters coverage_profiles planning_types desks stages"

log() { printf '>> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The dump + package routine, run inside the mongo container's shell. Emits the
# tarball at /tmp/superdesk-content-config.tgz. $SUBDIR names the folder the
# collections land under (cosmetic; convert.py globs for the first *.bson).
container_dump_script() {
  local subdir="$1"
  cat <<EOF
set -e
DB=${DB}
OUT=/tmp/ccdump_\$\$; PKG=/tmp/ccpkg_\$\$
rm -rf "\$OUT" "\$PKG"; mkdir -p "\$OUT" "\$PKG/superdesk-content-config"
for c in ${COLLECTIONS}; do
  mongodump --quiet --db "\$DB" --collection "\$c" --out "\$OUT" 2>/dev/null || true
done
mv "\$OUT/\$DB" "\$PKG/superdesk-content-config/${subdir}"
tar czf /tmp/superdesk-content-config.tgz -C "\$PKG" superdesk-content-config
rm -rf "\$OUT" "\$PKG"
EOF
}

dump_local() {
  local cid
  cid="${LOCAL_MONGO_CONTAINER:-$(docker ps --filter name=mongodb --format '{{.Names}}' | grep -i superdesk | head -1)}"
  [ -n "$cid" ] || die "no local superdesk mongo container found (set LOCAL_MONGO_CONTAINER)"
  log "Local mongo container: $cid"
  container_dump_script local | docker exec -i "$cid" bash -s
  docker cp "$cid":/tmp/superdesk-content-config.tgz "$OUT"
  docker exec "$cid" rm -f /tmp/superdesk-content-config.tgz
}

# --- deployed (SSM) helpers -------------------------------------------------------
ssm_run() { # $1 = shell snippet -> stdout of the invocation
  local snippet b64 params cid status
  b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  params=$(jq -n --arg b "$b64" '{commands:[("echo " + $b + " | base64 -d | bash")]}')
  cid=$(aws ssm send-command --region "$REGION" --instance-ids "$HOST" \
    --document-name AWS-RunShellScript --parameters "$params" \
    --query 'Command.CommandId' --output text)
  while :; do
    status=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
      --instance-id "$HOST" --query 'Status' --output text 2>/dev/null || echo Pending)
    case "$status" in
      Success) break ;;
      Failed|Cancelled|TimedOut|Undeliverable|Terminated)
        aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
          --instance-id "$HOST" --query 'StandardErrorContent' --output text >&2
        die "SSM command $status" ;;
    esac
    sleep 2
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" \
    --instance-id "$HOST" --query 'StandardOutputContent' --output text
}

dump_deployed() {
  local env="$1" cluster svc task ci total off end chunk tmpb64
  cluster="pesacheck-${env}-cluster"; svc="pesacheck-${env}-mongodb"
  aws sts get-caller-identity >/dev/null 2>&1 || die "not authenticated (run: aws login)"
  log "Resolving host for $svc on $cluster ..."
  task=$(aws ecs list-tasks --cluster "$cluster" --region "$REGION" \
    --service-name "$svc" --query 'taskArns[0]' --output text)
  [ "$task" != "None" ] && [ -n "$task" ] || die "no running task for $svc"
  ci=$(aws ecs describe-tasks --cluster "$cluster" --region "$REGION" \
    --tasks "$task" --query 'tasks[0].containerInstanceArn' --output text)
  HOST=$(aws ecs describe-container-instances --cluster "$cluster" --region "$REGION" \
    --container-instances "$ci" --query 'containerInstances[0].ec2InstanceId' --output text)
  log "Host: $HOST"

  # dump inside the container on the host, copy out, base64 on the host. The
  # inner container script is base64-encoded so no quoting survives to fight the
  # SSM/docker exec layers.
  local cscript_b64 snippet
  cscript_b64=$(container_dump_script "$env" | base64 | tr -d '\n')
  snippet=$(cat <<EOF
set -e
CID=\$(docker ps --filter name=pesacheck-${env}-mongodb -q | head -1)
[ -n "\$CID" ] || { echo "no mongo container" >&2; exit 1; }
echo ${cscript_b64} | base64 -d | docker exec -i "\$CID" bash
docker cp "\$CID":/tmp/superdesk-content-config.tgz /tmp/cc.tgz
docker exec "\$CID" rm -f /tmp/superdesk-content-config.tgz
base64 /tmp/cc.tgz | tr -d '\n' > /tmp/cc.b64
rm -f /tmp/cc.tgz
wc -c < /tmp/cc.b64
EOF
)
  log "Running mongodump (read-only) ..."
  total=$(ssm_run "$snippet" | tr -d '[:space:]')
  log "base64 length: $total; pulling in chunks (get-command-invocation caps stdout ~24KB) ..."
  chunk=18000; off=1; tmpb64=$(mktemp)
  while [ "$off" -le "$total" ]; do
    end=$(( off + chunk - 1 ))
    ssm_run "cut -c${off}-${end} /tmp/cc.b64" | tr -d '[:space:]' >> "$tmpb64"
    off=$(( end + 1 ))
  done
  ssm_run "rm -f /tmp/cc.b64" >/dev/null
  base64 -d < "$tmpb64" > "$OUT"
  rm -f "$tmpb64"
}

case "$SOURCE" in
  local)         dump_local ;;
  staging|prod)  dump_deployed "$SOURCE" ;;
  *)             die "usage: $0 <local|staging|prod> [OUT.tgz]" ;;
esac

log "Wrote $OUT"
log "Contents:"
tar tzf "$OUT" >&2
