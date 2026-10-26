locals {
  # Existing project-shared APIs; never disable these APIs on destroy.
  required_services = toset([
    "compute.googleapis.com",
    "iap.googleapis.com",
    "oslogin.googleapis.com",
    "serviceusage.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.required_services
  project  = var.project_id
  service  = each.value

  disable_on_destroy         = false
  disable_dependent_services = false

  lifecycle {
    prevent_destroy = true
  }
}

module "host" {
  source = "./modules/host"
  vm = {
    name         = var.vm_name
    zone         = var.zone
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb
    image        = var.boot_image
  }
  network = {
    name   = var.network_name
    region = var.region
    cidr   = var.subnet_cidr
  }
  labels           = merge({ purpose = "agent-development" }, var.labels)
  bootstrap_base64 = filebase64("${path.module}/../packages/long-running-agent/src/long_running_agent/runtime/bootstrap.py")

  depends_on = [google_project_service.required]
}

module "access" {
  source      = "./modules/access"
  project_id  = var.project_id
  group_email = var.access.login_group_email
  vm          = module.host.vm
}

module "backups" {
  source     = "./modules/backups"
  vm_name    = var.vm_name
  region     = var.region
  boot_disk  = module.host.boot_disk
  chain_name = var.snapshot_chain_name
  labels     = merge({ purpose = "agent-backup", cadence = "weekly" }, var.labels)

  depends_on = [google_project_service.required]
}
