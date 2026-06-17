# ── Network (VPC, IGW, subnet, route table, security group) ───────────────────
module "network" {
  source = "./modules/network"

  name_suffix       = local.name_suffix
  admin_source_cidr = var.admin_source_cidr
  tags              = local.common_tags
}

# ── EC2 instance ──────────────────────────────────────────────────────────────
module "vm" {
  source = "./modules/vm"

  name_suffix       = local.name_suffix
  instance_type     = local.effective_instance_type
  admin_username    = var.admin_username
  ssh_public_key    = var.ssh_public_key
  subnet_id         = module.network.subnet_id
  security_group_id = module.network.security_group_id
  # Render cloud-init template — passed to the instance as user_data.
  user_data = templatefile("${path.module}/cloud-init.tftpl", {
    demo              = var.demo
    bp_opamp_endpoint = var.bp_opamp_endpoint
    bp_secret_key     = var.bp_secret_key
    admin_username    = var.admin_username
  })
  tags = local.common_tags
}
