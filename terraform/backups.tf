# Low-churn recovery points: regional standard snapshots store changed blocks.
# A 14-day age policy usually retains two weekly points; Google only expires an
# old point after a newer one exists, so this is not a strict snapshot count.
resource "google_compute_resource_policy" "weekly_backup" {
  name        = "${var.vm_name}-weekly-backup"
  region      = var.region
  description = "Weekly regional boot-disk snapshots; 14-day retention; no guest pause"

  snapshot_schedule_policy {
    schedule {
      weekly_schedule {
        day_of_weeks {
          day        = "SUNDAY"
          start_time = "13:00"
        }
      }
    }

    retention_policy {
      max_retention_days    = 14
      on_source_disk_delete = "APPLY_RETENTION_POLICY"
    }

    snapshot_properties {
      storage_locations = [var.region]
      chain_name        = var.snapshot_chain_name
      guest_flush       = false
      labels            = merge({ purpose = "agent-backup", cadence = "weekly" }, var.labels)
    }
  }

  depends_on = [google_project_service.required]

  lifecycle {
    prevent_destroy = true
  }
}

# Separate attachment keeps the imported disk and VM untouched.
resource "google_compute_disk_resource_policy_attachment" "weekly_backup" {
  name = google_compute_resource_policy.weekly_backup.name
  disk = google_compute_disk.boot.name
  zone = google_compute_disk.boot.zone

  lifecycle {
    prevent_destroy = true
  }
}
