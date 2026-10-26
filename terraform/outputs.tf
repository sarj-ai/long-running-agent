output "vm" {
  value = merge(module.host.vm, {
    project = var.project_id
    connect = "vm terminal"
    ssh     = "vm ssh"
  })
}
