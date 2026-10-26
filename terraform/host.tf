resource "google_compute_network" "devbox" {
  name                                      = var.network_name
  description                               = "Isolated persistent agent host; SSH via IAP"
  auto_create_subnetworks                   = false
  routing_mode                              = "REGIONAL"
  network_firewall_policy_enforcement_order = "AFTER_CLASSIC_FIREWALL"

  depends_on = [google_project_service.required]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_subnetwork" "devbox" {
  name                     = "${var.network_name}-${var.region}"
  region                   = var.region
  network                  = google_compute_network.devbox.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = false
  purpose                  = "PRIVATE"
  stack_type               = "IPV4_ONLY"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.network_name}-allow-iap-ssh"
  network       = google_compute_network.devbox.id
  direction     = "INGRESS"
  priority      = 1000
  disabled      = false
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [var.network_name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_disk" "boot" {
  name = var.vm_name
  zone = var.zone
  type = "pd-balanced"
  size = var.disk_size_gb

  # Pin the imported image. OS patches happen in-place; a newer image must not
  # replace a disk containing code, browser sign-ins and agent history.
  image = var.boot_image

  depends_on = [google_project_service.required]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_instance" "devbox" {
  name                      = var.vm_name
  zone                      = var.zone
  machine_type              = var.machine_type
  allow_stopping_for_update = false
  deletion_protection       = true
  can_ip_forward            = false
  tags                      = [var.network_name]
  labels                    = merge({ purpose = "agent-development" }, var.labels)

  boot_disk {
    source      = google_compute_disk.boot.id
    auto_delete = false
    device_name = "persistent-disk-0"
    mode        = "READ_WRITE"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.devbox.id
    stack_type = "IPV4_ONLY"
    access_config {
      # Ephemeral address: released while stopped, no reservation charge.
      network_tier = "PREMIUM"
    }
  }

  metadata = {
    "enable-oslogin"         = "TRUE"
    "block-project-ssh-keys" = "TRUE"
    "serial-port-enable"     = "FALSE"
    "startup-script"         = <<-SCRIPT
      #!/bin/sh
      set -eu
      printf '%s' '${filebase64("${path.module}/../packages/long-running-agent/src/long_running_agent/runtime/bootstrap.py")}' | /usr/bin/base64 --decode | /usr/bin/python3 -I
    SCRIPT
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
    preemptible         = false
    provisioning_model  = "STANDARD"
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  # No service_account block: this VM has no attached identity.
  # No desired_status: Terraform must not wake a stopped VM.
  lifecycle {
    prevent_destroy = true
  }
}
