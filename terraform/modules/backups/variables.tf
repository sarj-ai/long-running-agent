variable "vm_name" {
  description = "Existing host name used for the weekly snapshot policy."
  type        = string
}

variable "region" {
  description = "Region containing the source disk and its snapshots."
  type        = string
}

variable "boot_disk" {
  description = "Existing persistent disk receiving the snapshot schedule."
  type = object({
    name = string
    zone = string
  })
}

variable "chain_name" {
  description = "Explicit immutable snapshot-chain identity for this deployment."
  type        = string
}

variable "labels" {
  description = "Nonsecret snapshot labels supplied by the deployment."
  type        = map(string)
  default     = {}
}
