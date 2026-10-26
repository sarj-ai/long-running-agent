moved {
  from = google_compute_network.devbox
  to   = module.host.google_compute_network.devbox
}

moved {
  from = google_compute_subnetwork.devbox
  to   = module.host.google_compute_subnetwork.devbox
}

moved {
  from = google_compute_firewall.iap_ssh
  to   = module.host.google_compute_firewall.iap_ssh
}

moved {
  from = google_compute_disk.boot
  to   = module.host.google_compute_disk.boot
}

moved {
  from = google_compute_instance.devbox
  to   = module.host.google_compute_instance.devbox
}

moved {
  from = google_compute_instance_iam_member.agent_vm_login
  to   = module.access.google_compute_instance_iam_member.agent_vm_login
}

moved {
  from = google_iap_tunnel_instance_iam_member.agent_vm_ssh
  to   = module.access.google_iap_tunnel_instance_iam_member.agent_vm_ssh
}

moved {
  from = google_project_iam_custom_role.agent_vm_discovery
  to   = module.access.google_project_iam_custom_role.agent_vm_discovery
}

moved {
  from = google_project_iam_member.agent_vm_discovery
  to   = module.access.google_project_iam_member.agent_vm_discovery
}

moved {
  from = google_project_iam_audit_config.iap_access
  to   = module.access.google_project_iam_audit_config.iap_access
}

moved {
  from = google_compute_resource_policy.weekly_backup
  to   = module.backups.google_compute_resource_policy.weekly_backup
}

moved {
  from = google_compute_disk_resource_policy_attachment.weekly_backup
  to   = module.backups.google_compute_disk_resource_policy_attachment.weekly_backup
}
