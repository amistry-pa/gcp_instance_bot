# gcp_instance_bot

A Cloud Function + Cloud Scheduler bot that retries GCP VM provisioning until it succeeds, then emails you. Built for GPU stockouts (H100, A100, etc.) where availability is intermittent.

## What it does

Every 5 minutes the bot tries, **in parallel across all configured zones**, to bring a named VM to `RUNNING`. First zone to succeed wins; duplicate successes in other zones are auto-deleted. When one succeeds, the bot emails the recipients and pauses itself.

## Modes

- **`snapshot`** — If the named instance exists and is stopped, start it. If it doesn't exist in a zone, create it there from `SOURCE_SNAPSHOT`. Use this to resume interrupted work.
- **`fresh`** — Create a brand-new instance from a base OS image. Ignores existing stopped instances with the same name.

## Prerequisites

- GCP project with quota for the target machine type
- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- Gmail account with an [app password](https://myaccount.google.com/apppasswords) (needs 2FA enabled)

## Setup

```bash
git clone https://github.com/amistry-pa/gcp_instance_bot.git
cd gcp_instance_bot
cp env.yaml.example env.yaml
```

Edit `env.yaml` — required fields: `GCP_PROJECT_ID`, `INSTANCE_NAME`, `PREFERRED_ZONES`, `MACHINE_TYPE`, `MODE`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `NOTIFICATION_EMAIL_TO`. If `MODE=snapshot`, also set `SOURCE_SNAPSHOT`.

Edit the config block at the top of `deploy.sh`: `PROJECT_ID`, `REGION`, `FUNCTION_NAME`, `SCHEDULER_JOB_NAME`.

## Using snapshot mode

Create a snapshot of your existing VM's boot disk first:

```bash
gcloud compute snapshots create MY-SNAPSHOT \
    --source-disk=MY-INSTANCE \
    --source-disk-zone=us-central1-a \
    --storage-location=us-central1 \
    --project=MY-PROJECT
```

Then set `MODE: "snapshot"` and `SOURCE_SNAPSHOT: "MY-SNAPSHOT"` in `env.yaml`.

## Using fresh mode

Set `MODE: "fresh"` in `env.yaml`. Configure `BOOT_IMAGE_PROJECT` / `BOOT_IMAGE_FAMILY` if you want something other than Debian 12.

## Deploy

```bash
bash deploy.sh
```

The script enables APIs, creates a service account with required IAM, deploys the Cloud Function (gen2), and schedules it to run every 5 minutes.

## Manage

```bash
# Pause
gcloud scheduler jobs pause SCHEDULER_JOB_NAME --location=REGION --project=PROJECT_ID

# Resume
gcloud scheduler jobs resume SCHEDULER_JOB_NAME --location=REGION --project=PROJECT_ID

# Trigger manually
gcloud scheduler jobs run SCHEDULER_JOB_NAME --location=REGION --project=PROJECT_ID

# View logs
gcloud functions logs read FUNCTION_NAME --region=REGION --project=PROJECT_ID --limit=30
```

## How parallel mode handles duplicates

All zones are attempted concurrently. The first zone whose operation completes claims the "winner" slot. Slower zones that also succeed are handled as follows:

- **Created instance (slower create)** → deleted automatically
- **Started existing instance (slower start)** → left running (user can stop manually)

This means `fresh` and `snapshot`-without-existing-stopped cases are fully self-cleaning. The only case that requires manual cleanup is if multiple zones had the same stopped instance (which isn't possible since instance names are zone-scoped — in practice you'll only have this situation in `us-central1-a` where the original lives).

## Configuration reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GCP_PROJECT_ID` | yes | — | |
| `INSTANCE_NAME` | yes | — | |
| `PREFERRED_ZONES` | yes | — | comma-separated, e.g. `us-central1-a,us-central1-b` |
| `MACHINE_TYPE` | yes | — | e.g. `a3-highgpu-8g`, `a2-highgpu-1g` |
| `MODE` | yes | `snapshot` | `snapshot` or `fresh` |
| `SOURCE_SNAPSHOT` | if MODE=snapshot | — | snapshot name (global scope) |
| `BOOT_DISK_SIZE_GB` | no | `200` | auto-bumped to snapshot size if smaller |
| `BOOT_DISK_TYPE` | no | `pd-ssd` | |
| `BOOT_IMAGE_PROJECT` | no | `debian-cloud` | only used in fresh mode |
| `BOOT_IMAGE_FAMILY` | no | `debian-12` | only used in fresh mode |
| `GMAIL_ADDRESS` | yes | — | |
| `GMAIL_APP_PASSWORD` | yes | — | 16-char app password |
| `NOTIFICATION_EMAIL_TO` | yes | — | comma-separated |
| `SCHEDULER_REGION` | no | `us-central1` | |
| `SCHEDULER_JOB_NAME` | no | — | empty disables auto-pause |

## Troubleshooting

- **Build fails with Artifact Registry denied** — grant `roles/artifactregistry.writer` to the project's `{project-number}-compute@developer.gserviceaccount.com` (deploy.sh does this automatically).
- **Scheduler invocations return 401** — the bot's service account needs `roles/run.invoker` on the specific Cloud Run service backing the function (deploy.sh does this).
- **`Requested disk size cannot be smaller than the snapshot size`** — the bot auto-bumps the disk size to match the snapshot. If you still hit this, check that `SOURCE_SNAPSHOT` resolves correctly.
- **Email not arriving** — verify 2FA is on for the Gmail account and you're using an app password, not the account password.
