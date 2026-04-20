import logging
import os
import smtplib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock

import functions_framework
import google.auth
import google.auth.transport.requests
from google.api_core import exceptions as gcp_exceptions
from google.cloud import compute_v1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gcp-instance-bot")

RUNNING_STATES = {"RUNNING", "STAGING", "PROVISIONING"}
STOPPED_STATES = {"TERMINATED", "STOPPED", "STOPPING", "SUSPENDED"}

RETRYABLE_ERROR_SUBSTRINGS = [
    "ZONE_RESOURCE_POOL_EXHAUSTED",
    "QUOTA_EXCEEDED",
    "stockout",
    "does not have enough resources available",
]


class Winner:
    """Thread-safe holder for the first zone that successfully provisions."""

    def __init__(self):
        self._lock = Lock()
        self.zone = None
        self.action = None

    def try_claim(self, zone: str, action: str) -> bool:
        with self._lock:
            if self.zone is None:
                self.zone = zone
                self.action = action
                return True
            return False

    def claimed(self) -> bool:
        with self._lock:
            return self.zone is not None


def build_instance(
    name: str,
    zone: str,
    machine_type: str,
    boot_disk_size_gb: int,
    source_image_link: str | None,
    source_snapshot_link: str | None,
    disk_type: str,
) -> compute_v1.Instance:
    disk_params = compute_v1.AttachedDiskInitializeParams(
        disk_size_gb=boot_disk_size_gb,
        disk_type=f"zones/{zone}/diskTypes/{disk_type}",
    )
    if source_snapshot_link:
        disk_params.source_snapshot = source_snapshot_link
    else:
        disk_params.source_image = source_image_link

    return compute_v1.Instance(
        name=name,
        machine_type=f"zones/{zone}/machineTypes/{machine_type}",
        disks=[compute_v1.AttachedDisk(
            initialize_params=disk_params,
            auto_delete=True,
            boot=True,
        )],
        network_interfaces=[compute_v1.NetworkInterface(
            network="global/networks/default",
            access_configs=[compute_v1.AccessConfig(
                type_=compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name,
                name="External NAT",
                network_tier="PREMIUM",
            )],
        )],
        scheduling=compute_v1.Scheduling(
            on_host_maintenance=compute_v1.Scheduling.OnHostMaintenance.TERMINATE.name,
            automatic_restart=True,
        ),
    )


def provision_in_zone(
    project: str,
    zone: str,
    instance_name: str,
    mode: str,
    machine_type: str,
    boot_disk_size_gb: int,
    disk_type: str,
    source_image_link: str | None,
    source_snapshot_link: str | None,
    winner: Winner,
) -> dict:
    """Attempt to provision a single zone. Returns a status dict."""
    client = compute_v1.InstancesClient()

    try:
        instance = client.get(project=project, zone=zone, instance=instance_name)
        status = instance.status
    except gcp_exceptions.NotFound:
        status = None

    if status in RUNNING_STATES:
        winner.try_claim(zone, "already_running")
        return {"zone": zone, "action": "already_running", "success": True}

    if status in STOPPED_STATES:
        if mode != "snapshot":
            return {"zone": zone, "action": "skipped_existing_in_fresh_mode", "success": False}
        if winner.claimed():
            return {"zone": zone, "action": "skipped_another_winner", "success": False}
        try:
            logger.info(f"[{zone}] starting existing instance")
            op = client.start(project=project, zone=zone, instance=instance_name)
            op.result(timeout=300)
        except Exception as e:
            return _classify_error(zone, "start", e)
        if winner.try_claim(zone, "started"):
            return {"zone": zone, "action": "started", "success": True}
        # Another worker already won; leave this running, user can stop later
        return {"zone": zone, "action": "started_but_duplicate", "success": True}

    # status is None -> instance doesn't exist here, create it
    if winner.claimed():
        return {"zone": zone, "action": "skipped_another_winner", "success": False}

    resource = build_instance(
        name=instance_name,
        zone=zone,
        machine_type=machine_type,
        boot_disk_size_gb=boot_disk_size_gb,
        source_image_link=source_image_link,
        source_snapshot_link=source_snapshot_link,
        disk_type=disk_type,
    )
    request = compute_v1.InsertInstanceRequest(
        project=project, zone=zone, instance_resource=resource,
    )
    try:
        logger.info(f"[{zone}] creating instance ({'snapshot' if source_snapshot_link else 'fresh'})")
        op = client.insert(request=request)
        op.result(timeout=300)
    except gcp_exceptions.Conflict:
        # Race with another process; treat as existing
        return {"zone": zone, "action": "conflict", "success": False}
    except Exception as e:
        return _classify_error(zone, "create", e)

    if winner.try_claim(zone, "created"):
        return {"zone": zone, "action": "created", "success": True}

    # Another zone won first. Delete this duplicate.
    logger.warning(f"[{zone}] won but another zone beat us; deleting duplicate")
    try:
        del_op = client.delete(project=project, zone=zone, instance=instance_name)
        del_op.result(timeout=300)
        return {"zone": zone, "action": "deleted_duplicate", "success": False}
    except Exception as e:
        logger.error(f"[{zone}] failed to delete duplicate: {e}")
        return {"zone": zone, "action": "duplicate_delete_failed", "success": False, "error": str(e)}


def _classify_error(zone: str, op_type: str, exc: Exception) -> dict:
    err = str(exc)
    if any(s in err for s in RETRYABLE_ERROR_SUBSTRINGS):
        logger.warning(f"[{zone}] {op_type} retryable: {err}")
        return {"zone": zone, "action": f"{op_type}_stocked_out", "success": False, "error": err}
    logger.error(f"[{zone}] {op_type} non-retryable: {err}")
    return {"zone": zone, "action": f"{op_type}_error", "success": False, "error": err}


def resolve_source(
    mode: str,
    project: str,
    snapshot_name: str | None,
    boot_image_project: str,
    boot_image_family: str,
    boot_disk_size_gb: int,
) -> tuple[str | None, str | None, int]:
    """Return (image_link, snapshot_link, effective_disk_size_gb)."""
    if mode == "snapshot":
        if not snapshot_name:
            raise ValueError("MODE=snapshot requires SOURCE_SNAPSHOT to be set")
        snap = compute_v1.SnapshotsClient().get(project=project, snapshot=snapshot_name)
        effective_size = max(boot_disk_size_gb, snap.disk_size_gb or 0)
        return None, snap.self_link, effective_size
    image = compute_v1.ImagesClient().get_from_family(
        project=boot_image_project, family=boot_image_family
    )
    return image.self_link, None, boot_disk_size_gb


def send_email(subject: str, html: str) -> None:
    user = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to_raw = os.environ.get("NOTIFICATION_EMAIL_TO", "")
    recipients = [e.strip() for e in to_raw.split(",") if e.strip()]
    if not all([user, pw, recipients]):
        logger.error("Email configuration incomplete; skipping")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(user, pw)
            server.sendmail(user, recipients, msg.as_string())
        logger.info(f"Email sent to {recipients}")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


def pause_scheduler(project: str, region: str, job_name: str) -> None:
    try:
        creds, _ = google.auth.default()
        creds.refresh(google.auth.transport.requests.Request())
        url = (
            f"https://cloudscheduler.googleapis.com/v1/projects/{project}"
            f"/locations/{region}/jobs/{job_name}:pause"
        )
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {creds.token}")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req)
        logger.info(f"Scheduler job {job_name} paused")
    except Exception as e:
        logger.error(f"Scheduler pause failed: {e}")


@functions_framework.http
def provision_instance(request):
    """Cloud Function entry point. Triggered by Cloud Scheduler."""
    project = os.environ["GCP_PROJECT_ID"]
    instance_name = os.environ["INSTANCE_NAME"]
    zones = [z.strip() for z in os.environ["PREFERRED_ZONES"].split(",") if z.strip()]
    mode = os.environ.get("MODE", "snapshot").lower()
    machine_type = os.environ.get("MACHINE_TYPE", "a3-highgpu-8g")
    boot_disk_size = int(os.environ.get("BOOT_DISK_SIZE_GB", "200"))
    disk_type = os.environ.get("BOOT_DISK_TYPE", "pd-ssd")
    boot_image_project = os.environ.get("BOOT_IMAGE_PROJECT", "debian-cloud")
    boot_image_family = os.environ.get("BOOT_IMAGE_FAMILY", "debian-12")
    snapshot_name = os.environ.get("SOURCE_SNAPSHOT", "").strip() or None
    scheduler_region = os.environ.get("SCHEDULER_REGION", "us-central1")
    scheduler_job = os.environ.get("SCHEDULER_JOB_NAME", "")

    if mode not in ("snapshot", "fresh"):
        return (f"Invalid MODE={mode}; must be 'snapshot' or 'fresh'", 400)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info(f"[{now}] mode={mode} instance={instance_name} zones={zones}")

    try:
        image_link, snapshot_link, effective_disk_size = resolve_source(
            mode, project, snapshot_name, boot_image_project,
            boot_image_family, boot_disk_size,
        )
    except ValueError as e:
        return (str(e), 400)

    winner = Winner()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(zones))) as pool:
        futures = {
            pool.submit(
                provision_in_zone,
                project, zone, instance_name, mode, machine_type,
                effective_disk_size, disk_type, image_link, snapshot_link, winner,
            ): zone
            for zone in zones
        }
        for future in as_completed(futures):
            results.append(future.result())

    if winner.zone:
        console_url = (
            f"https://console.cloud.google.com/compute/instancesDetail/"
            f"zones/{winner.zone}/instances/{instance_name}?project={project}"
        )
        source_desc = f"from snapshot <b>{snapshot_name}</b>" if snapshot_link else "fresh"
        action_label = winner.action.replace("_", " ")
        send_email(
            f"[GCP Bot] {instance_name} {action_label} in {winner.zone}",
            f"<h2>GPU Instance Ready</h2>"
            f"<p>Instance <b>{instance_name}</b> (<code>{machine_type}</code>) "
            f"{action_label} {source_desc} in <b>{winner.zone}</b> at {now}.</p>"
            f"<p><a href='{console_url}'>View in Console</a></p>"
            f"<p>Zone results: <pre>{results}</pre></p>"
            f"<p><i>The retry bot has been automatically paused.</i></p>",
        )
        if scheduler_job:
            pause_scheduler(project, scheduler_region, scheduler_job)
        return (f"Winner: {winner.zone} ({winner.action})", 200)

    # All zones failed. Distinguish stockout from hard errors.
    errors = [r for r in results if r.get("action", "").endswith("_error")]
    if errors:
        send_email(
            f"[GCP Bot] ERROR provisioning {instance_name}",
            f"<h2>Provisioning Error</h2>"
            f"<p>Non-retryable errors across zones:</p>"
            f"<pre>{errors}</pre>"
            f"<p>Timestamp: {now}</p>",
        )
        return (f"Non-retryable errors: {errors}", 500)

    logger.warning(f"All zones exhausted: {results}")
    return ("All zones exhausted, will retry next cycle", 200)
