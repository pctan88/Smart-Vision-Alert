#!/bin/bash
# Smart Vision Alert — Google Cloud one-time setup
# Run this ONCE from your local machine after installing gcloud CLI.
# Usage: bash setup_gcloud.sh

set -e

PROJECT="itc-monitoring-495910"
REGION="asia-southeast1"
SERVICE="smart-vision-alert"
REPO="smart-vision-alert"
BUCKET="itc-monitoring-sva-session"
SESSION_BLOB="xiaomi_session.json"

echo "=== Smart Vision Alert — Cloud Setup ==="
echo "Project : $PROJECT"
echo "Region  : $REGION"
echo ""

# 1. Set project
gcloud config set project $PROJECT

# 2. Enable required APIs
echo "[1/7] Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com

# 3. Create Artifact Registry repo for Docker images
echo "[2/7] Creating Artifact Registry repository..."
gcloud artifacts repositories create $REPO \
  --repository-format=docker \
  --location=$REGION \
  --description="Smart Vision Alert container images" \
  || echo "  (already exists, skipping)"

# 4. Create GCS bucket for Xiaomi session
echo "[3/7] Creating GCS bucket for session storage..."
gcloud storage buckets create gs://$BUCKET \
  --location=$REGION \
  --uniform-bucket-level-access \
  || echo "  (already exists, skipping)"

# 5. Create secrets in Secret Manager
echo "[4/7] Creating secrets..."
echo "  Enter values when prompted. Press Ctrl+C to skip any and set manually."

create_secret() {
  local name=$1
  local prompt=$2
  if gcloud secrets describe $name --project=$PROJECT &>/dev/null; then
    echo "  $name already exists — skipping"
  else
    echo -n "  $prompt: "
    read -s val
    echo ""
    echo -n "$val" | gcloud secrets create $name \
      --data-file=- \
      --replication-policy=automatic \
      --project=$PROJECT
    echo "  ✓ $name created"
  fi
}

create_secret "GEMINI_API_KEY"      "GEMINI_API_KEY"
create_secret "TELEGRAM_BOT_TOKEN"  "TELEGRAM_BOT_TOKEN"
create_secret "TELEGRAM_CHAT_ID"    "TELEGRAM_CHAT_ID"
create_secret "STUDIO_CAMERAS"      "STUDIO_CAMERAS (JSON array)"
create_secret "CLOUD_RUN_SECRET"    "CLOUD_RUN_SECRET (shared trigger token)"
create_secret "INTERNAL_SECRET"     "INTERNAL_SECRET (Cloud Run → A2 callback token)"
create_secret "A2_BASE_URL"         "A2_BASE_URL (e.g. https://yourdomain.com)"
# XIAOMI_USERNAME / XIAOMI_PASSWORD not needed — session loaded from GCS

# 6. Grant Cloud Build permission to deploy Cloud Run
echo "[5/7] Setting IAM permissions..."
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/run.admin" --quiet

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/iam.serviceAccountUser" --quiet

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${CB_SA}" \
  --role="roles/artifactregistry.writer" --quiet

# Grant Cloud Run service account access to Secret Manager and GCS
CR_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${CR_SA}" \
  --role="roles/secretmanager.secretAccessor" --quiet

gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:${CR_SA}" \
  --role="roles/storage.objectAdmin"

# 7. Deploy initial Cloud Run service with all secret env vars
echo "[6/7] Deploying Cloud Run service..."
gcloud run deploy $SERVICE \
  --region=$REGION \
  --platform=managed \
  --no-allow-unauthenticated \
  --timeout=540 \
  --memory=512Mi \
  --cpu=1 \
  --concurrency=1 \
  --max-instances=1 \
  --set-env-vars="GCS_BUCKET=${BUCKET},GCS_SESSION_BLOB=${SESSION_BLOB},XIAOMI_SERVER_REGION=sg,STUDIO_CAMERA_HOST=sg.business.smartcamera.api.io.mi.com,GEMINI_MODEL=gemini-2.5-flash,ALERT_THRESHOLD=medium,ALERT_COOLDOWN_MINUTES=15,LOG_LEVEL=INFO" \
  --set-secrets="\
GEMINI_API_KEY=GEMINI_API_KEY:latest,\
TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,\
TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest,\
STUDIO_CAMERAS=STUDIO_CAMERAS:latest,\
CLOUD_RUN_SECRET=CLOUD_RUN_SECRET:latest,\
INTERNAL_SECRET=INTERNAL_SECRET:latest,\
A2_BASE_URL=A2_BASE_URL:latest" \
  --source=.

echo ""
echo "[7/7] Getting Cloud Run service URL..."
SERVICE_URL=$(gcloud run services describe $SERVICE \
  --region=$REGION \
  --format='value(status.url)')

echo ""
echo "=== Setup Complete ==="
echo "Cloud Run URL : $SERVICE_URL"
echo ""
echo "Next steps:"
echo "  1. Add to A2 .env:  CLOUD_RUN_URL=$SERVICE_URL"
echo "  2. Add to A2 .env:  CLOUD_RUN_SECRET=<same value you entered above>"
echo "  3. Add to A2 .env:  INTERNAL_SECRET=<same value you entered above>"
echo "  4. Upload Xiaomi session: python3 migrate_session_to_gcs.py"
echo "  5. Connect GitHub repo in Cloud Build console for auto-deploy on push"
