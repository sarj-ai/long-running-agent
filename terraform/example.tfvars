# Copy to private/deployment.tfvars and replace every deployment value.
# For an existing host, preserve its exact identities, image, capacity and labels.
project_id   = "REPLACE_WITH_PROJECT_ID"
region       = "REPLACE_WITH_REGION"
zone         = "REPLACE_WITH_ZONE"
vm_name      = "REPLACE_WITH_EXISTING_VM_NAME"
machine_type = "REPLACE_WITH_MACHINE_TYPE"
disk_size_gb = 200
network_name = "REPLACE_WITH_EXISTING_NETWORK_NAME"
subnet_cidr  = "REPLACE_WITH_EXISTING_SUBNET_CIDR"
boot_image   = "projects/ubuntu-os-cloud/global/images/REPLACE_WITH_EXACT_IMAGE"
state_bucket = {
  name     = "REPLACE_WITH_EXISTING_STATE_BUCKET"
  location = "REPLACE_WITH_BUCKET_LOCATION"
}
access = {
  customer_id         = "REPLACE_WITH_CUSTOMER_ID"
  managed_group_email = "host-security@example.invalid"
  login_group_email   = "host-users@example.invalid"
}
snapshot_chain_name = "REPLACE_WITH_EXISTING_SNAPSHOT_CHAIN"
labels              = {}
