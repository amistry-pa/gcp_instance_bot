#!/usr/bin/env bash
# Deploy the GCP instance procurement retry bot.
#
# Prerequisites:
#   - gcloud CLI authenticated
#   - env.yaml present (copy from env.yaml.example and fill in your values)
#   - Gmail app password (https://myaccount.google.com/apppasswords)
set -euo pipefail

# ──────────────────────────────────────────────
# Configuration — EDIT THESE
# ──────────────────────────────────────────────
PROJECT_ID="your-project-id"
REGION="us-central1"
FUNCTION_NAME="gpu-provision-bot"
SCHEDULER_JOB_NAME="gpu-retry-scheduler"
SCHEDULE_CRON="*/5 * * * *"

# ──────────────────────────────────────────────
# Derived values
# ──────────────────────────────────────────────
SA_NAME="gpu-provisioner"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")

if [ ! -f env.yaml ]; then
    echo "ERROR: env.yaml not found. Copy env.yaml.example to env.yaml and fill it in."
    exit 1
fi

echo "=== [1/6] Enabling required APIs ==="
gcloud services enable \
    cloudfunctions.googleapis.com \
    cloudscheduler.googleapis.com \
    compute.googleapis.com \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    --project="${PROJECT_ID}" --quiet

echo "=== [2/6] Creating service account ==="
gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GPU Provisioner Bot" \
    --project="${PROJECT_ID}" 2>/dev/null || echo "Service account already exists."

echo "=== [3/6] Binding IAM roles ==="
for ROLE in \
    roles/compute.instanceAdmin.v1 \
    roles/iam.serviceAccountUser \
    roles/logging.logWriter \
    roles/cloudfunctions.invoker \
    roles/run.invoker \
    roles/cloudscheduler.admin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="${ROLE}" \
        --condition=None --quiet >/dev/null
done

# Cloud Build (via Compute default SA) needs Artifact Registry write for gen2 functions
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/artifactregistry.writer" \
    --condition=None --quiet >/dev/null

echo "=== [4/6] Deploying Cloud Function ==="
gcloud functions deploy "${FUNCTION_NAME}" \
    --gen2 \
    --region="${REGION}" \
    --runtime=python312 \
    --source=. \
    --entry-point=provision_instance \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account="${SA_EMAIL}" \
    --timeout=540s \
    --memory=512Mi \
    --max-instances=1 \
    --env-vars-file=env.yaml \
    --project="${PROJECT_ID}" --quiet

# Grant run.invoker on the specific Cloud Run service (for OIDC auth)
gcloud run services add-iam-policy-binding "${FUNCTION_NAME}" \
    --region="${REGION}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/run.invoker" \
    --project="${PROJECT_ID}" --quiet >/dev/null

echo "=== [5/6] Creating Cloud Scheduler job ==="
FUNCTION_URL=$(gcloud functions describe "${FUNCTION_NAME}" \
    --gen2 --region="${REGION}" --project="${PROJECT_ID}" \
    --format="value(serviceConfig.uri)")

gcloud scheduler jobs delete "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" --project="${PROJECT_ID}" \
    --quiet 2>/dev/null || true

gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --schedule="${SCHEDULE_CRON}" \
    --uri="${FUNCTION_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SA_EMAIL}" \
    --oidc-token-audience="${FUNCTION_URL}" \
    --attempt-deadline="540s" \
    --project="${PROJECT_ID}" --quiet

echo "=== [6/6] Done ==="
echo ""
echo "Function URL: ${FUNCTION_URL}"
echo "Scheduler:    ${SCHEDULER_JOB_NAME} (${SCHEDULE_CRON})"
echo ""
echo "Commands:"
echo "  Pause:   gcloud scheduler jobs pause ${SCHEDULER_JOB_NAME} --location=${REGION} --project=${PROJECT_ID}"
echo "  Resume:  gcloud scheduler jobs resume ${SCHEDULER_JOB_NAME} --location=${REGION} --project=${PROJECT_ID}"
echo "  Trigger: gcloud scheduler jobs run ${SCHEDULER_JOB_NAME} --location=${REGION} --project=${PROJECT_ID}"
echo "  Logs:    gcloud functions logs read ${FUNCTION_NAME} --region=${REGION} --project=${PROJECT_ID} --limit=50"
