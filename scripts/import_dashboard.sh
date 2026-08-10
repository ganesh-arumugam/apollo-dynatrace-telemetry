#!/usr/bin/env bash
# Import dashboards/dynatrace-dashboard.json into a Dynatrace tenant.
#
# The new Platform dashboards live in the Document Store, NOT the classic
# /api/v2/dashboards API, and the Document API wants a **Bearer** token — not the
# Api-Token used for OTLP ingest. Two credentials therefore exist side by side:
#
#   ingest      Authorization: Api-Token dt0c01...   (metrics/traces/logs.ingest)
#   dashboards  Authorization: Bearer    dt0s16...   (document:documents:write)
#
# Getting that wrong is a 401 that looks like an ingest problem. Ask for the
# right one up front.
#
# ── Option A: Platform token (recommended) ───────────────────────────────────
#   https://myaccount.dynatrace.com/platformTokens → Platform token
#   scope: document:documents:write        (prefix dt0s16)
#   .env:  DT_BEARER_TOKEN=dt0s16.XXXX.YYYY
#
# ── Option B: OAuth2 client credentials (CI / service account) ───────────────
#   myaccount.dynatrace.com → Identity & access management → OAuth clients
#   grant type: client credentials, permission: document:documents:write
#   .env:  DT_OAUTH_CLIENT_ID=dt0s02.XXXX
#          DT_OAUTH_CLIENT_SECRET=YYYY
#          DT_ACCOUNT_UUID=<account uuid>   <- the token is scoped by this
#
# Also required: DT_ENVIRONMENT_ID (e.g. abc12345)
#
# Full click-path for both: docs/dynatrace-credentials.md
#
# Usage: ./scripts/import_dashboard.sh [dashboard.json] [--name "..."]
#
# The auth flow, delete-then-create behaviour, and the .apps.-host requirement
# were all verified against a live tenant.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
DASHBOARD_FILE="${1:-$ROOT/dashboards/dynatrace-dashboard.json}"
DASHBOARD_NAME="${DASHBOARD_NAME:-Apollo Router — Telemetry}"

# Load .env if the caller didn't export the vars.
if [ -z "${DT_ENVIRONMENT_ID:-}" ] || \
   { [ -z "${DT_BEARER_TOKEN:-}" ] && [ -z "${DT_OAUTH_CLIENT_ID:-}" ]; }; then
  if [ -f "$ROOT/.env" ]; then
    echo "Loading credentials from $ROOT/.env"
    set -a; . "$ROOT/.env"; set +a
  fi
fi

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -n "${DT_ENVIRONMENT_ID:-}" ] || fail "DT_ENVIRONMENT_ID is not set (e.g. abc12345)"
[ -f "$DASHBOARD_FILE" ] || fail "dashboard file not found: $DASHBOARD_FILE"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DASHBOARD_FILE" \
  || fail "$DASHBOARD_FILE is not valid JSON"

# Regenerate check: importing a stale dashboard is a silent way to lose edits.
# Exit 1 means genuinely stale; exit 2 means the checker itself could not run
# (usually no PyYAML), which must not be reported as staleness.
python3 "$HERE/build_dashboard.py" --check >/dev/null 2>&1
case "$?" in
  0) ;;
  1) echo "WARNING: dashboards/dynatrace-dashboard.json is stale relative to"
     echo "         dashboards/tiles.yaml. Run scripts/build_dashboard.py first if"
     echo "         you meant to import the reviewed queries." ;;
  *) echo "NOTE: could not verify the dashboard is current (build_dashboard.py"
     echo "      --check did not run; PyYAML missing?). Importing as-is." ;;
esac

DT_LIVE="https://${DT_ENVIRONMENT_ID}.live.dynatrace.com"
DT_APPS="https://${DT_ENVIRONMENT_ID}.apps.dynatrace.com"
# The document service is served by the .apps. host only. The .live. host answers
# but rejects these paths at the proxy with an HTML 403 ("administrative rules"),
# which reads like a scope problem and is not one - same trap as DT023 on ingest.
# v1 is the current version; override if your tenant still serves v0.
ENDPOINT="${DT_APPS}/platform/document/${DOCUMENT_API_VERSION:-v1}/documents"

echo "Importing $(basename "$DASHBOARD_FILE") -> ${ENDPOINT}"

# ── Resolve a Bearer token ───────────────────────────────────────────────────
BEARER=""
if [ -n "${DT_BEARER_TOKEN:-}" ]; then
  BEARER="$DT_BEARER_TOKEN"
  echo "  auth: platform token (DT_BEARER_TOKEN)"
elif [ -n "${DT_OAUTH_CLIENT_ID:-}" ] && [ -n "${DT_OAUTH_CLIENT_SECRET:-}" ]; then
  echo "  auth: OAuth2 client credentials"
  # The `resource` parameter must be urn:dtaccount:<account-uuid>. Passing an
  # environment URL here yields a token that 403s on every environment API, which
  # reads like a permissions problem rather than a malformed token request.
  [ -n "${DT_ACCOUNT_UUID:-}" ] || fail "DT_ACCOUNT_UUID is required for the OAuth flow.
       Find it in the Account Management URL or under Account settings — or ask
       the SSO endpoint, which will tell you. Exchange the client credentials
       WITHOUT a resource parameter and decode the 'res' claim of the JWT:

         curl -s -X POST https://sso.dynatrace.com/sso/oauth2/token \\
           -d grant_type=client_credentials \\
           -d client_id=\$DT_OAUTH_CLIENT_ID \\
           -d client_secret=\$DT_OAUTH_CLIENT_SECRET \\
           -d scope=document:documents:write \\
         | python3 -c 'import sys,json,base64; t=json.load(sys.stdin)[\"access_token\"].split(\".\")[1]; \\
             print(json.loads(base64.urlsafe_b64decode(t+\"=\"*(-len(t)%4)))[\"res\"])'

       It prints urn:dtaccount:<uuid>. See docs/dynatrace-credentials.md."
  TOKEN_RESPONSE=$(curl -s --max-time 15 -X POST "https://sso.dynatrace.com/sso/oauth2/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=client_credentials" \
    --data-urlencode "client_id=${DT_OAUTH_CLIENT_ID}" \
    --data-urlencode "client_secret=${DT_OAUTH_CLIENT_SECRET}" \
    --data-urlencode "scope=${DT_OAUTH_SCOPE:-document:documents:write}" \
    --data-urlencode "resource=urn:dtaccount:${DT_ACCOUNT_UUID}" 2>/dev/null \
    || echo '{"error":"curl_failed"}')
  BEARER=$(python3 -c "
import json,sys
d=json.loads(sys.stdin.read() or '{}')
t=d.get('access_token','')
print(t if t else 'ERR:'+str(d.get('error_description') or d.get('error','unknown')))" \
    <<<"$TOKEN_RESPONSE")
  case "$BEARER" in
    ERR:*) fail "OAuth2 token request failed: ${BEARER#ERR:}
       Simpler path: create a platform token with scope document:documents:write at
       https://myaccount.dynatrace.com/platformTokens and set DT_BEARER_TOKEN." ;;
  esac
else
  fail "no dashboard credential found. Either:
         DT_BEARER_TOKEN     a platform token (dt0s16) with document:documents:write
                             from https://myaccount.dynatrace.com/platformTokens
         DT_OAUTH_CLIENT_ID + DT_OAUTH_CLIENT_SECRET + DT_ACCOUNT_UUID
       The OTLP ingest Api-Token will NOT work here — this API wants Bearer.
       Step-by-step: docs/dynatrace-credentials.md"
fi

# ── Delete any previous copies ───────────────────────────────────────────────
# The document API has no upsert: POST always mints a new id, so re-importing
# accumulates identically-named dashboards and you cannot tell which one is
# current. Delete every document with this name first, then create one.
# `filter=name='...'` returns nothing here, so list and match client-side.
echo "  checking for existing '${DASHBOARD_NAME}'..."
EXISTING=$(curl -s --max-time 30 -G "$ENDPOINT" --data-urlencode "page-size=200" \
  -H "Authorization: Bearer ${BEARER}" \
  | DASHBOARD_NAME="$DASHBOARD_NAME" python3 -c "
import json, os, sys
want = os.environ['DASHBOARD_NAME']
try:
    docs = json.load(sys.stdin).get('documents', [])
except Exception:
    sys.exit(0)
for d in docs:
    if d.get('name') == want and d.get('type') == 'dashboard':
        # DELETE rejects a request without the document's current version
        # ('optimistic-locking-version must be specified'), so carry it along.
        print(f\"{d['id']}:{d.get('version', 1)}\")
")

if [ -n "$EXISTING" ]; then
  for entry in $EXISTING; do
    old="${entry%%:*}"; ver="${entry##*:}"
    code=$(curl -s --max-time 30 -o /tmp/dt_dash_delete.json -w '%{http_code}' \
      -X DELETE "${ENDPOINT}/${old}?optimistic-locking-version=${ver}" \
      -H "Authorization: Bearer ${BEARER}")
    case "$code" in
      200|204) echo "  deleted previous dashboard ${old} (v${ver})" ;;
      *)       echo "  WARNING: could not delete ${old} (HTTP ${code}) — you may end up with a duplicate"
               head -c 200 /tmp/dt_dash_delete.json 2>/dev/null | sed 's/^/           /' ;;
    esac
  done
else
  echo "  none found — this is a fresh import"
fi

# ── Upload ───────────────────────────────────────────────────────────────────
STATUS=$(curl -s --max-time 30 -o /tmp/dt_dash_import.json -w '%{http_code}' \
  -X POST "$ENDPOINT" \
  -H "Authorization: Bearer ${BEARER}" \
  -F "name=${DASHBOARD_NAME}" \
  -F "type=dashboard" \
  -F "content=@${DASHBOARD_FILE};type=application/json")
  # `content` must be a multipart *file* part (@), not a plain field read from a
  # file (<). With `<` the API replies 400 "Required part 'content' is not present".

case "$STATUS" in
  200|201)
    DOC_ID=$(python3 -c "
import json
try:
    d=json.load(open('/tmp/dt_dash_import.json')); print(d.get('id', d.get('documentId','unknown')))
except Exception: print('unknown')")
    echo "  imported OK (document ${DOC_ID})"
    echo "  open: ${DT_APPS}/ui/apps/dynatrace.dashboards/${DOC_ID}"
    ;;
  401|403)
    echo "  auth error ${STATUS}:" >&2
    python3 -m json.tool </tmp/dt_dash_import.json 2>/dev/null >&2 || cat /tmp/dt_dash_import.json >&2
    echo "  Needs scope document:documents:write. A platform token also only works" >&2
    echo "  within its owning user's permissions, and an OAuth token must have been" >&2
    echo "  requested with resource=urn:dtaccount:<account-uuid>." >&2
    echo "  See docs/dynatrace-credentials.md." >&2
    exit 1 ;;
  409)
    echo "  a dashboard named \"${DASHBOARD_NAME}\" already exists (409)." >&2
    echo "  Delete it or re-run with DASHBOARD_NAME=\"...\"." >&2
    exit 1 ;;
  *)
    echo "  unexpected HTTP ${STATUS}:" >&2
    python3 -m json.tool </tmp/dt_dash_import.json 2>/dev/null >&2 || cat /tmp/dt_dash_import.json >&2
    exit 1 ;;
esac
