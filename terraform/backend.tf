terraform {
  # Supply bucket and prefix through an ignored private backend configuration.
  backend "gcs" {}
}

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket.name
  project                     = var.project_id
  location                    = var.state_bucket.location
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = merge({ purpose = "terraform-state" }, var.labels)

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = 30
      num_newer_versions         = 10
      with_state                 = "ARCHIVED"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
