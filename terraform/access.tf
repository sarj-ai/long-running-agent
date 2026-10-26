resource "google_cloud_identity_group" "agent_vm_access" {
  provider        = google.identity
  parent          = "customers/${var.access.customer_id}"
  display_name    = "Agent VM access"
  description     = "Named users authorized for personal OS Login and agent workspaces on the shared agent VM. Membership does not grant VM administrator or Docker privileges."
  deletion_policy = "PREVENT"

  group_key {
    id = var.access.managed_group_email
  }

  labels = {
    "cloudidentity.googleapis.com/groups.discussion_forum" = ""
    "cloudidentity.googleapis.com/groups.security"         = ""
  }

  lifecycle {
    prevent_destroy = true
    # Cloud Identity omits this creation-only setting when reading imported groups.
    ignore_changes = [initial_group_config]
  }
}
