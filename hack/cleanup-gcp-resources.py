#!/usr/bin/env python3
"""
GCP CI Resource Cleanup

Deletes GCP resources created by OpenShift CI jobs (ci-op prefix) that are
older than --max-age-hours (default: 6). Follows the dependency order
required to avoid deletion failures.

Deletion sequence:
  1. TargetTcpProxy → BackendService (global) → HealthCheck
  2. DNS ManagedZone  (non-NS/SOA record sets first, then the zone)
  3. Storage Buckets
  4. FirewallRule → InstanceGroup → Subnetwork → Network

Usage:
  # See what would be deleted without touching anything
  python3 hack/cleanup-gcp-resources.py --project openshift-observability --dry-run

  # Live run
  python3 hack/cleanup-gcp-resources.py --project openshift-observability

  # Adjust age threshold and enable verbose output
  python3 hack/cleanup-gcp-resources.py --project openshift-observability --max-age-hours 4 --debug

Environment (for CI / non-interactive use):
  GOOGLE_APPLICATION_CREDENTIALS  Path to a GCP service-account JSON key file.
  GCP_PROJECT                     Project ID (overrides --project when set).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

CI_NAME_PREFIX = "ci-op"
DEFAULT_MAX_AGE_HOURS = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[str, int]:
    """Run *cmd* and return (stdout, returncode). Never raises."""
    log.debug("+ %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        log.error("Command not found: %s — is gcloud installed and on PATH?", cmd[0])
        return "", 127
    if result.returncode != 0:
        log.debug("stderr: %s", result.stderr.strip())
    return result.stdout.strip(), result.returncode


def gcloud_list(*args, project: str) -> list[dict]:
    """Run a `gcloud … list` command and return the parsed JSON array."""
    cmd = ["gcloud", *args, "--format=json", "--quiet", "--project", project]
    stdout, rc = _run(cmd)
    if rc != 0 or not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.debug("JSON parse error from gcloud: %s", exc)
        return []


def url_last_segment(url: str) -> str:
    """Return the last path component of a GCP resource URL (e.g. zone, region)."""
    return url.split("/")[-1] if url else ""


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an RFC-3339 / ISO-8601 timestamp into a UTC-aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def is_older_than(timestamp_str: str | None, hours: int) -> bool:
    """
    Return True when the resource is older than *hours*.
    Resources with unparseable timestamps are skipped (treated as new).
    """
    ts = parse_timestamp(timestamp_str)
    if ts is None:
        log.warning("Unable to parse timestamp %r; treating as new (skipped)", timestamp_str)
        return False
    return datetime.now(timezone.utc) - ts > timedelta(hours=hours)


def is_ci_resource(name: str) -> bool:
    return name.startswith(CI_NAME_PREFIX)


# ---------------------------------------------------------------------------
# Cleaner
# ---------------------------------------------------------------------------

class GCPCleaner:
    def __init__(self, project: str, dry_run: bool, max_age_hours: int) -> None:
        self.project = project
        self.dry_run = dry_run
        self.max_age_hours = max_age_hours
        self.deleted = 0
        self.skipped = 0
        self.errors = 0

    # -- eligibility ---------------------------------------------------------

    def _eligible(self, name: str, timestamp_str: str | None) -> bool:
        """Return True when this resource should be targeted for deletion."""
        if not is_ci_resource(name):
            log.debug("Skip %-55s  (not a CI resource)", name)
            self.skipped += 1
            return False
        if not is_older_than(timestamp_str, self.max_age_hours):
            log.info("Skip %-55s  (newer than %dh)", name, self.max_age_hours)
            self.skipped += 1
            return False
        return True

    # -- deletion ------------------------------------------------------------

    def _delete(self, label: str, cmd: list[str]) -> bool:
        """
        Execute *cmd* (or simulate it when dry_run is set).
        *cmd* must NOT include --quiet or --project; those are appended here.
        """
        full_cmd = cmd + ["--quiet", "--project", self.project]
        if self.dry_run:
            log.info("[DRY-RUN] Would delete %s", label)
            self.deleted += 1
            return True
        log.info("Deleting %s", label)
        _, rc = _run(full_cmd)
        if rc != 0:
            log.error("FAILED to delete %s", label)
            self.errors += 1
            return False
        self.deleted += 1
        return True

    # -- resource handlers (called in dependency order) ----------------------

    def clean_target_tcp_proxies(self) -> None:
        log.info("=== TargetTcpProxy ===")
        for item in gcloud_list("compute", "target-tcp-proxies", "list", project=self.project):
            name = item.get("name", "")
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"TargetTcpProxy/{name}",
                    ["gcloud", "compute", "target-tcp-proxies", "delete", name],
                )

    def clean_backend_services(self) -> None:
        # Global backend services (most CI-created LBs use global)
        log.info("=== BackendService (global) ===")
        for item in gcloud_list("compute", "backend-services", "list", "--global", project=self.project):
            name = item.get("name", "")
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"BackendService/global/{name}",
                    ["gcloud", "compute", "backend-services", "delete", name, "--global"],
                )

        # Regional backend services (internal LBs land here)
        log.info("=== BackendService (regional) ===")
        for item in gcloud_list("compute", "backend-services", "list", project=self.project):
            # gcloud returns both global and regional without --global; skip any already handled globally
            if item.get("region") is None:
                continue
            name = item.get("name", "")
            region = url_last_segment(item.get("region", ""))
            if not region:
                log.warning("BackendService %s has no region, skipping", name)
                continue
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"BackendService/{region}/{name}",
                    ["gcloud", "compute", "backend-services", "delete", name, "--region", region],
                )

    def clean_health_checks(self) -> None:
        log.info("=== HealthCheck ===")
        for item in gcloud_list("compute", "health-checks", "list", project=self.project):
            name = item.get("name", "")
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"HealthCheck/{name}",
                    ["gcloud", "compute", "health-checks", "delete", name],
                )

    def clean_dns_zones(self) -> None:
        log.info("=== DNS ManagedZone ===")
        for zone in gcloud_list("dns", "managed-zones", "list", project=self.project):
            name = zone.get("name", "")
            if not self._eligible(name, zone.get("creationTime")):
                continue
            # Record sets must be fully removed before the zone can be deleted.
            # Only proceed with zone deletion when all records were cleaned up.
            records_ok = self._delete_dns_records(name)
            if records_ok:
                self._delete(
                    f"ManagedZone/{name}",
                    ["gcloud", "dns", "managed-zones", "delete", name],
                )
            else:
                log.warning("Skipping zone deletion for %s — some record sets could not be removed", name)

    def _delete_dns_records(self, zone: str) -> bool:
        """Delete all non-NS/SOA records from *zone*. Returns True when all succeeded."""
        records = gcloud_list("dns", "record-sets", "list", "--zone", zone, project=self.project)
        all_ok = True
        for record in records:
            rtype = record.get("type", "")
            if rtype in ("NS", "SOA"):
                continue
            rname = record.get("name", "")
            ok = self._delete(
                f"DNSRecord/{zone}/{rtype}/{rname}",
                ["gcloud", "dns", "record-sets", "delete", rname, "--zone", zone, "--type", rtype],
            )
            if not ok:
                all_ok = False
        return all_ok

    def clean_storage_buckets(self) -> None:
        log.info("=== Storage Bucket ===")
        for item in gcloud_list("storage", "buckets", "list", project=self.project):
            # gcloud may return the name as "gs://bucket-name" or just "bucket-name"
            raw = item.get("name", "")
            name = raw.removeprefix("gs://").rstrip("/")
            ts = item.get("timeCreated") or item.get("createTime")
            if not self._eligible(name, ts):
                continue
            # gcloud storage rm does not accept --project or --quiet flags;
            # call _run() directly rather than routing through _delete().
            label = f"Bucket/{name}"
            if self.dry_run:
                log.info("[DRY-RUN] Would delete %s", label)
                self.deleted += 1
            else:
                log.info("Deleting %s", label)
                _, rc = _run(["gcloud", "storage", "rm", "--recursive", f"gs://{name}"])
                if rc != 0:
                    log.error("FAILED to delete %s", label)
                    self.errors += 1
                else:
                    self.deleted += 1

    def clean_firewall_rules(self) -> None:
        log.info("=== FirewallRule ===")
        for item in gcloud_list("compute", "firewall-rules", "list", project=self.project):
            name = item.get("name", "")
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"FirewallRule/{name}",
                    ["gcloud", "compute", "firewall-rules", "delete", name],
                )

    def clean_instance_groups(self) -> None:
        log.info("=== InstanceGroup (managed) ===")
        for item in gcloud_list("compute", "instance-groups", "managed", "list", project=self.project):
            name = item.get("name", "")
            zone = url_last_segment(item.get("zone", ""))
            if not zone:
                log.warning("ManagedInstanceGroup %s has no zone, skipping", name)
                continue
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"ManagedInstanceGroup/{zone}/{name}",
                    ["gcloud", "compute", "instance-groups", "managed", "delete", name, "--zone", zone],
                )

        log.info("=== InstanceGroup (unmanaged) ===")
        for item in gcloud_list("compute", "instance-groups", "unmanaged", "list", project=self.project):
            name = item.get("name", "")
            zone = url_last_segment(item.get("zone", ""))
            if not zone:
                log.warning("UnmanagedInstanceGroup %s has no zone, skipping", name)
                continue
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"UnmanagedInstanceGroup/{zone}/{name}",
                    ["gcloud", "compute", "instance-groups", "unmanaged", "delete", name, "--zone", zone],
                )

    def clean_subnetworks(self) -> None:
        log.info("=== Subnetwork ===")
        for item in gcloud_list("compute", "networks", "subnets", "list", project=self.project):
            name = item.get("name", "")
            region = url_last_segment(item.get("region", ""))
            if not region:
                log.warning("Subnetwork %s has no region, skipping", name)
                continue
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"Subnetwork/{region}/{name}",
                    ["gcloud", "compute", "networks", "subnets", "delete", name, "--region", region],
                )

    def clean_networks(self) -> None:
        log.info("=== Network ===")
        for item in gcloud_list("compute", "networks", "list", project=self.project):
            name = item.get("name", "")
            if name == "default":
                log.debug("Skip default network (never deleted)")
                continue
            if self._eligible(name, item.get("creationTimestamp")):
                self._delete(
                    f"Network/{name}",
                    ["gcloud", "compute", "networks", "delete", name],
                )

    # -- entry point ---------------------------------------------------------

    def run(self) -> bool:
        log.info(
            "Starting cleanup  project=%s  dry_run=%s  max_age=%dh",
            self.project, self.dry_run, self.max_age_hours,
        )

        # Dependency-ordered: each group must finish before the next starts.
        self.clean_target_tcp_proxies()
        self.clean_backend_services()
        self.clean_health_checks()
        self.clean_dns_zones()
        self.clean_storage_buckets()
        self.clean_firewall_rules()
        self.clean_instance_groups()
        self.clean_subnetworks()
        self.clean_networks()

        log.info(
            "Done — deleted=%d  skipped=%d  errors=%d",
            self.deleted, self.skipped, self.errors,
        )
        return self.errors == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT", ""),
        help="GCP project ID (or set GCP_PROJECT env var). Default: %(default)r",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without making any changes.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        metavar="N",
        help="Skip resources newer than N hours. Default: %(default)s",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (prints every gcloud command).",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.project:
        parser.error("--project is required (or set the GCP_PROJECT environment variable)")

    cleaner = GCPCleaner(args.project, args.dry_run, args.max_age_hours)
    sys.exit(0 if cleaner.run() else 1)


if __name__ == "__main__":
    main()
