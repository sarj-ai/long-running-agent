output "vm" {
  description = "Protected host identity, current capacity and terminal connection commands."
  value = {
    project      = var.project_id
    zone         = google_compute_instance.devbox.zone
    name         = google_compute_instance.devbox.name
    machine_type = google_compute_instance.devbox.machine_type
    boot_disk_gb = google_compute_disk.boot.size
    connect      = "vm terminal"
    ssh          = "vm ssh"
  }
}
