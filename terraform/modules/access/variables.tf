variable "project_id" {
  description = "Existing project receiving additive access bindings and IAP audit logging."
  type        = string
}

variable "vm" {
  description = "The only VM to which this module grants OS Login and SSH access."
  type = object({
    name = string
    zone = string
  })
}

variable "group_email" {
  description = "Explicit group address receiving ordinary OS Login and SSH-only IAP access."
  type        = string
}
