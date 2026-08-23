#!/usr/bin/env bash
#
# deploy_cloudrun.sh — deploy Daleel to Google Cloud Run.
#
#   bash scripts/deploy_cloudrun.sh
#
# Checks everything that can be checked BEFORE spending 15 minutes on a build,
# because every one of these failures otherwise surfaces at the end:
#
#   - gcloud missing, or signed out
#   - no project selected
#   - required APIs not enabled
#   - the secret missing, or unreadable by Cloud Run's service account
#
# That last one is the classic: the deploy succeeds, the container starts, and
# then dies with "LLM_API_KEY is missing" because Cloud Run was told to inject
# a secret it has no permission to read.

set -euo pipefail

SERVICE="${SERVICE:-daleel}"
REGION="${REGION:-me-central1}"          # Doha — closest region to Oman
MEMORY="${MEMORY:-4Gi}"                  # app needs ~2.2Gi; headroom is free when idle
CPU="${CPU:-2}"                          # model loading is CPU-bound
CONCURRENCY="${CONCURRENCY:-20}"         # default 80 shares one Python process too widely
MAX_INSTANCES="${MAX_INSTANCES:-3}"      # a hard ceiling on spend
TIMEOUT="${TIMEOUT:-120}"
SECRET="${SECRET:-LLM_API_KEY}"
MODELS="${MODELS:-gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { red "FAILED: $*"; exit 1; }

echo
echo "Daleel -> Cloud Run"
echo "==================="

# ---- preflight -------------------------------------------------------------

command -v gcloud >/dev/null 2>&1 \
  || die "gcloud not found. Install: https://cloud.google.com/sdk/docs/install"

ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
[ -n "$ACCOUNT" ] || die "not signed in. Run:  gcloud auth login"

PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
[ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] \
  || die "no project set. Run:  gcloud config set project YOUR_PROJECT_ID"

info "account : $ACCOUNT"
info "project : $PROJECT"
info "service : $SERVICE  ($REGION, $MEMORY, $CPU vCPU)"
echo

echo "Enabling required APIs (no-op if already on)..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --quiet

# ---- the secret ------------------------------------------------------------

if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  red "Secret '$SECRET' does not exist."
  echo
  echo "Create it, pasting the key at the prompt then Ctrl-D:"
  echo "    gcloud secrets create $SECRET --data-file=-"
  exit 1
fi
grn "secret '$SECRET' exists"

# Cloud Run runs as the compute service account unless told otherwise. It
# needs explicit read access; granting is idempotent, so just do it.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null
grn "secret readable by $RUNTIME_SA"

# ---- sanity: is there anything to serve? -----------------------------------

[ -f data/index/embeddings.npy ] && [ -f data/index/chunks.json ] \
  || die "data/index is missing. Run:  python scripts/build_index.py"
[ -f data/registry/authorities.json ] \
  || die "data/registry/authorities.json is missing."
grn "index and registry present"

echo
echo "Building and deploying — first run takes 10-20 minutes."
echo "(installing torch and baking in the 1.1 GB embedding model)"
echo

# ---- deploy ----------------------------------------------------------------

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --memory "$MEMORY" \
  --cpu "$CPU" \
  --timeout "$TIMEOUT" \
  --concurrency "$CONCURRENCY" \
  --max-instances "$MAX_INSTANCES" \
  --allow-unauthenticated \
  --set-secrets "LLM_API_KEY=${SECRET}:latest" \
  --set-env-vars "LLM_MODEL=${MODELS}"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" \
        --format='value(status.url)')"

echo
grn "deployed: $URL"
echo
echo "Checking health..."
curl -fsS "${URL}/healthz" || red "  /healthz did not respond — check: gcloud run services logs tail $SERVICE --region $REGION"
echo
echo
echo "Try a real question:"
echo "  curl -X POST ${URL}/chat -H 'Content-Type: application/json' \\"
echo "    -d '{\"history\":[{\"role\":\"user\",\"content\":\"كم رسوم تجديد جواز السفر؟\"}]}'"
echo
echo "Cold starts: the instance is dropped after ~15 idle minutes and the next"
echo "visitor waits for the model to load. Before the demo, either open the URL"
echo "a minute early, or keep one warm for the day:"
echo "  gcloud run services update $SERVICE --region $REGION --min-instances 1"
echo "  gcloud run services update $SERVICE --region $REGION --min-instances 0   # after"
echo
