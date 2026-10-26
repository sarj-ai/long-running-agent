terraform {
  required_version = "= 1.17.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 8.7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Adopt existing labels exactly; importing must not add a provider label.
  add_terraform_attribution_label = false
}

provider "google" {
  alias                           = "identity"
  project                         = var.project_id
  billing_project                 = var.project_id
  user_project_override           = true
  add_terraform_attribution_label = false
}
