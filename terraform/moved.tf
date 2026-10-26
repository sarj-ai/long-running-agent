# Accept state from the former pass-through modules; original root addresses remain unchanged.

moved {
  from = module.host.google_compute_network.devbox
  to   = google_compute_network.devbox
}

moved {
  from = module.host.google_compute_subnetwork.devbox
  to   = google_compute_subnetwork.devbox
}

moved {
  from = module.host.google_compute_firewall.iap_ssh
  to   = google_compute_firewall.iap_ssh
}

moved {
  from = module.host.google_compute_disk.boot
  to   = google_compute_disk.boot
}

moved {
  from = module.host.google_compute_instance.devbox
  to   = google_compute_instance.devbox
}

moved {
  from = module.access.google_compute_instance_iam_member.agent_vm_login
  to   = google_compute_instance_iam_member.agent_vm_login
}

moved {
  from = module.access.google_iap_tunnel_instance_iam_member.agent_vm_ssh
  to   = google_iap_tunnel_instance_iam_member.agent_vm_ssh
}

moved {
  from = module.access.google_project_iam_custom_role.agent_vm_discovery
  to   = google_project_iam_custom_role.agent_vm_discovery
}

moved {
  from = module.access.google_project_iam_member.agent_vm_discovery
  to   = google_project_iam_member.agent_vm_discovery
}

moved {
  from = module.access.google_project_iam_audit_config.iap_access
  to   = google_project_iam_audit_config.iap_access
}

moved {
  from = module.backups.google_compute_resource_policy.weekly_backup
  to   = google_compute_resource_policy.weekly_backup
}

moved {
  from = module.backups.google_compute_disk_resource_policy_attachment.weekly_backup
  to   = google_compute_disk_resource_policy_attachment.weekly_backup
}
