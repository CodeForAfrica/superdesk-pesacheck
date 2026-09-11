#!/usr/bin/env bash
# Dump the authored editorial pages behind the curated Publisher content lists
# from a running Superdesk into JSON, ready to feed to convert.py.
#
# These pages (source=newsdesk) are the About/FAQ/Team/Ecosystem/Media-Centre
# content that curated content lists point at. They are re-authored + published at
# bootstrap so they return to Publisher with their original guids — the capture
# half of the curated-list-membership work (tier 3). Read-only against Mongo.
#
# The guid set to capture is READ FROM the Publisher membership tree
# (content_list_items.json) so the two always agree — only pages an actual list
# references are tracked. Ghost-sourced items in those lists are skipped (they
# come back via ingest, not fixtures).
#
# Usage:
#   ./dump.sh local   [OUT_DIR] [MEMBERSHIP_JSON]
#   ./dump.sh staging [OUT_DIR] [MEMBERSHIP_JSON]
#   ./dump.sh prod    [OUT_DIR] [MEMBERSHIP_JSON]
#
# Writes pages.json + pictures.json into OUT_DIR (default ./editorial-dump).
# Media bytes are pulled by a separate step (see dump_media, TODO) into
# data/editorial/media/ and tracked as regular git blobs.
set -euo pipefail

SOURCE="${1:-}"
OUT="${2:-./editorial-dump}"
DEFAULT_MEMBERSHIP="$(cd "$(dirname "$0")/../../../../superdesk-web-publisher/etc/docker/bootstrap/publisher-config" 2>/dev/null && pwd)/content_list_items.json"
MEMBERSHIP="${3:-$DEFAULT_MEMBERSHIP}"
REGION="${REGION:-eu-west-1}"
DB="${DB:-superdesk}"

log() { printf '>> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -f "$MEMBERSHIP" ] || die "membership file not found: $MEMBERSHIP"
GUIDS_JSON="$(python3 -c "import json,sys; d=json.load(open('$MEMBERSHIP')); print(json.dumps(sorted({i['guid'] for v in d.values() for i in v})))")"
mkdir -p "$OUT"

# mongosh program: given the guid array, export the latest published (or archive)
# doc per guid whose source is NOT Ghost (authored only) as pages, and their
# featuremedia picture items as pictures. Emitted as two JSON arrays to files in
# the container, then copied out.
build_js() {
  cat <<EOF
var wanted = ${GUIDS_JSON};
function latest(g){ return db.published.find({guid:g}).sort({versioncreated:-1}).limit(1).toArray()[0] || db.archive.findOne({guid:g}); }
var pages=[], pics=[], picSeen={};
wanted.forEach(function(g){
  var d=latest(g); if(!d) return;
  if(d.source==="Ghost") return;            // ingested, not a fixture
  pages.push(d);
  // Capture EVERY association picture, not just featuremedia: images embedded in
  // body_html are stored as associations keyed by their editor block id
  // (editor_0, editor_1, ...). Missing these leaves the body <img> pointing at
  // the authoring-time upload-raw URL (auth-gated) and the image renders blank.
  var assoc=d.associations||{};
  Object.keys(assoc).forEach(function(k){
    var a=assoc[k]; if(!a) return;
    var pg=a.guid||a._id; if(!pg||picSeen[pg]) return; picSeen[pg]=1;
    var p=latest(pg)||(a.type?a:null); if(p) pics.push(p);
  });
});
print(JSON.stringify({pages:pages, pictures:pics}));
EOF
}

run_local() {
  local cid; cid="$(docker ps --filter name=mongodb --format '{{.Names}}' | grep -i superdesk | head -1)"
  [ -n "$cid" ] || die "no local superdesk mongo container"
  log "Local mongo: $cid"
  build_js | docker exec -i "$cid" mongosh --quiet "$DB" > "$OUT/_raw.json"
}

ssm_run() {
  local b64 params cid status
  b64=$(printf '%s' "$1" | base64 | tr -d '\n')
  params=$(jq -n --arg b "$b64" '{commands:[("echo " + $b + " | base64 -d | bash")]}')
  cid=$(aws ssm send-command --region "$REGION" --instance-ids "$HOST" --document-name AWS-RunShellScript --parameters "$params" --query 'Command.CommandId' --output text)
  while :; do status=$(aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$HOST" --query 'Status' --output text 2>/dev/null || echo Pending); case "$status" in Success) break;; Failed|Cancelled|TimedOut|Undeliverable|Terminated) aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$HOST" --query 'StandardErrorContent' --output text >&2; die "SSM $status";; esac; sleep 2; done
  aws ssm get-command-invocation --region "$REGION" --command-id "$cid" --instance-id "$HOST" --query 'StandardOutputContent' --output text
}

run_deployed() {
  local env="$1" cluster svc task ci total off end chunk tmp jsb64 snippet
  cluster="pesacheck-${env}-cluster"; svc="pesacheck-${env}-mongodb"
  aws sts get-caller-identity >/dev/null 2>&1 || die "not authenticated (aws login)"
  task=$(aws ecs list-tasks --cluster "$cluster" --region "$REGION" --service-name "$svc" --query 'taskArns[0]' --output text)
  ci=$(aws ecs describe-tasks --cluster "$cluster" --region "$REGION" --tasks "$task" --query 'tasks[0].containerInstanceArn' --output text)
  HOST=$(aws ecs describe-container-instances --cluster "$cluster" --region "$REGION" --container-instances "$ci" --query 'containerInstances[0].ec2InstanceId' --output text)
  log "Host: $HOST"
  jsb64=$(build_js | base64 | tr -d '\n')
  snippet=$(cat <<EOS
set -e
CID=\$(docker ps --filter name=pesacheck-${env}-mongodb -q | head -1)
[ -n "\$CID" ] || { echo "no mongo container" >&2; exit 1; }
echo ${jsb64} | base64 -d | docker exec -i "\$CID" mongosh --quiet ${DB} | base64 | tr -d '\n' > /tmp/ed.b64
wc -c < /tmp/ed.b64
EOS
)
  log "Exporting authored pages (read-only) ..."
  total=$(ssm_run "$snippet" | tr -d '[:space:]')
  chunk=18000; off=1; tmp=$(mktemp)
  while [ "$off" -le "$total" ]; do end=$(( off + chunk - 1 )); ssm_run "cut -c${off}-${end} /tmp/ed.b64" | tr -d '[:space:]' >> "$tmp"; off=$(( end + 1 )); done
  ssm_run "rm -f /tmp/ed.b64" >/dev/null
  base64 -d < "$tmp" > "$OUT/_raw.json"; rm -f "$tmp"
}

case "$SOURCE" in
  local) run_local ;;
  staging|prod) run_deployed "$SOURCE" ;;
  *) die "usage: $0 <local|staging|prod> [OUT_DIR] [MEMBERSHIP_JSON]" ;;
esac

# Split the combined payload into pages.json / pictures.json for convert.py.
# mongosh over stdin echoes REPL prompts, so pull the single JSON line the
# program printed (the one starting with {"pages") rather than trusting the
# whole file to be clean JSON.
python3 - "$OUT" <<'PY'
import json, re, sys
out = sys.argv[1]
raw = open(f"{out}/_raw.json", encoding="utf-8").read()
m = re.search(r'\{"pages":.*\}', raw, re.S)
if not m:
    sys.exit("no JSON payload found in mongosh output")
data = json.loads(m.group(0))
json.dump(data.get("pages", []), open(f"{out}/pages.json", "w"))
json.dump(data.get("pictures", []), open(f"{out}/pictures.json", "w"))
print(f">> pages={len(data.get('pages',[]))} pictures={len(data.get('pictures',[]))}", file=sys.stderr)
PY
log "Wrote $OUT/pages.json and $OUT/pictures.json"

# Pull the ORIGINAL media bytes for each captured picture into the tracked
# media dir (regular git blobs). Deployed media is S3 at
# s3://pesacheck-media-<env>/superdesk/<original.media>; only the original is
# kept (Superdesk regenerates renditions on import). Local media is GridFS and is
# not captured here — the fixtures are always captured from a deployed instance.
if [ "$SOURCE" = "local" ]; then
  log "NOTE: local media is GridFS; capture fixtures from staging/prod for media."
else
  MEDIA_DEST="$(cd "$(dirname "$0")/../../data/editorial" && pwd)/media"
  mkdir -p "$MEDIA_DEST"
  BUCKET="pesacheck-media-${SOURCE}"
  log "Pulling original media from s3://$BUCKET/superdesk/ ..."
  python3 - "$OUT/pictures.json" <<'PY' > "$OUT/_media.txt"
import json, sys
ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
       "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg"}
for p in json.load(open(sys.argv[1])):
    o = (p.get("renditions") or {}).get("original") or {}
    key = o.get("media")
    if not key:
        continue
    e = ext.get((p.get("mimetype") or o.get("mimetype") or "image/jpeg").lower(), "jpg")
    print(f"{p['guid']} {key} {e}")
PY
  mcount=0
  while read -r guid key e; do
    [ -n "$guid" ] || continue
    aws s3 cp "s3://$BUCKET/superdesk/$key" "$MEDIA_DEST/$guid.$e" --quiet \
      && mcount=$((mcount + 1)) || log "  missing media: $guid ($key)"
  done < "$OUT/_media.txt"
  log "Pulled $mcount picture file(s) into $MEDIA_DEST"

  # Inline body images pasted as raw `<img src="…upload-raw/<date>/<id>…">`
  # (no association) are not captured above. convert.py normalises them into
  # editor embeds keyed by the upload-raw media id, so pull those bytes by id.
  # Skip ids already pulled as an association picture (same image, different name).
  log "Pulling inline body-image media from s3://$BUCKET/superdesk/ ..."
  python3 - "$OUT/pages.json" "$OUT/_media.txt" <<'PY' > "$OUT/_body_media.txt"
import json, re, sys
assoc = set()
for line in open(sys.argv[2]):
    parts = line.split()
    if len(parts) >= 2:
        assoc.add(parts[1].rsplit("/", 1)[-1])  # association original-media objectid
seen = set()
for d in json.load(open(sys.argv[1])):
    for m in re.finditer(r"upload-raw/(?:(\d+)/)?([0-9a-f]{24})", d.get("body_html") or ""):
        date, mid = m.group(1), m.group(2)
        if mid in assoc or mid in seen:
            continue
        seen.add(mid)
        print(f"{mid} {(date + '/' + mid) if date else mid}")
PY
  bcount=0
  while read -r mid key; do
    [ -n "$mid" ] || continue
    ls "$MEDIA_DEST/$mid".* >/dev/null 2>&1 && continue
    ct=$(aws s3api head-object --bucket "$BUCKET" --key "superdesk/$key" \
      --query ContentType --output text 2>/dev/null || echo "")
    case "$ct" in
      image/png) e=png ;; image/jpeg|image/jpg) e=jpg ;; image/webp) e=webp ;;
      image/gif) e=gif ;; image/svg+xml) e=svg ;; *) e=jpg ;;
    esac
    aws s3 cp "s3://$BUCKET/superdesk/$key" "$MEDIA_DEST/$mid.$e" --quiet \
      && bcount=$((bcount + 1)) || log "  missing body media: $mid ($key)"
  done < "$OUT/_body_media.txt"
  log "Pulled $bcount inline body-image file(s) into $MEDIA_DEST"
fi
