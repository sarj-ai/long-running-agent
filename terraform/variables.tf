variable "project_id" {
  description = "Explicit project receiving additive VM access and IAP audit bindings; unrelated IAM and billing stay unchanged."
  type        = string
}

variable "region" {
  description = "Region of the existing network and snapshot schedule."
  type        = string
}

variable "zone" {
  description = "Zone of the persistent host and boot disk."
  type        = string
}

variable "vm_name" {
  description = "Exact name of the existing host and boot disk."
  type        = string
}

variable "machine_type" {
  description = "Host capacity. Changes require a deliberate stopped-VM maintenance window."
  type        = string
}

variable "disk_size_gb" {
  description = "Persistent disk capacity in GiB; use the current size or larger."
  type        = number

  validation {
    condition     = var.disk_size_gb >= 200 && floor(var.disk_size_gb) == var.disk_size_gb
    error_message = "Use a whole number of GiB, at least 200, matching or exceeding the existing disk size."
  }
}

variable "network_name" {
  description = "Exact name of the dedicated VPC and its host network tag."
  type        = string
}

variable "subnet_cidr" {
  description = "Existing subnet range; changing it requires an explicit network migration."
  type        = string
}

variable "boot_image" {
  description = "Exact immutable boot-image reference from the existing disk; never use a mutable image family."
  type        = string

  validation {
    condition     = can(regex("^projects/[^/]+/global/images/[^/]+$", var.boot_image))
    error_message = "Supply an exact projects/PROJECT/global/images/IMAGE reference, not an image family."
  }
}

variable "state_bucket" {
  description = "Existing protected state bucket; configure its same name separately during backend initialization."
  type = object({
    name     = string
    location = string
  })
}

variable "access" {
  description = "Organization customer ID, managed security-group address, and group receiving host access."
  type = object({
    customer_id         = string
    managed_group_email = string
    login_group_email   = string
  })
}

variable "snapshot_chain_name" {
  description = "Exact existing snapshot-chain name; preserve it when adopting an existing host."
  type        = string
}

variable "labels" {
  description = "Optional nonsecret ownership labels; supply existing labels when adopting a deployment."
  type        = map(string)
  default     = {}
}
