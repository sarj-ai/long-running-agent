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
