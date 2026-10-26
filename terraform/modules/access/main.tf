locals {
  agent_vm_group_principal = "group:${var.group_email}"
}

resource "google_compute_instance_iam_member" "agent_vm_login" {
  project       = var.project_id
  zone          = var.vm.zone
  instance_name = var.vm.name
  role          = "roles/compute.osLogin"
  member        = local.agent_vm_group_principal

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_iap_tunnel_instance_iam_member" "agent_vm_ssh" {
  project  = var.project_id
  zone     = var.vm.zone
  instance = var.vm.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = local.agent_vm_group_principal

  condition {
    title       = "agent-vm-ssh-only"
    description = "Allow this access group to tunnel SSH to the agent VM."
    expression  = "destination.port == 22"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "agent_vm_discovery" {
  project         = var.project_id
  role_id         = "agentVmProjectDiscovery"
  title           = "Agent VM project discovery"
  description     = "Read project identity and Compute project settings for explicit-zone OS Login SSH."
  stage           = "GA"
  deletion_policy = "PREVENT"
  permissions = [
    "compute.projects.get",
    "resourcemanager.projects.get",
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_member" "agent_vm_discovery" {
  project = var.project_id
  role    = google_project_iam_custom_role.agent_vm_discovery.name
  member  = local.agent_vm_group_principal

  lifecycle {
    prevent_destroy = true
  }
}

# Own only IAP auditing, not the project's other APIs or access bindings.
resource "google_project_iam_audit_config" "iap_access" {
  project = var.project_id
  service = "iap.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
