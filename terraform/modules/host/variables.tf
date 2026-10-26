variable "vm" {
  description = "Persistent host identity and capacity; changes must preserve its imported disk."
  type = object({
    name         = string
    zone         = string
    machine_type = string
    disk_size_gb = number
    image        = string
  })
}

variable "network" {
  description = "Dedicated VPC, regional subnet and IAP-only SSH ingress."
  type = object({
    name   = string
    region = string
    cidr   = string
  })
}

variable "bootstrap_base64" {
  description = "Encoded source-owned host bootstrap, resolved by the root module."
  type        = string
}

variable "labels" {
  description = "Nonsecret host labels supplied by the deployment."
  type        = map(string)
  default     = {}
}
