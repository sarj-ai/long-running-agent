output "vm" {
  value = {
    zone         = google_compute_instance.devbox.zone
    name         = google_compute_instance.devbox.name
    machine_type = google_compute_instance.devbox.machine_type
    boot_disk_gb = google_compute_disk.boot.size
  }
}

output "boot_disk" {
  value = {
    name = google_compute_disk.boot.name
    zone = google_compute_disk.boot.zone
  }
}
