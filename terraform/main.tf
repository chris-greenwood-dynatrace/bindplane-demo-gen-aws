resource "azurerm_resource_group" "this" {
  name     = local.resource_group_name
  location = var.location
  tags     = local.common_tags
}

# ── Network (VNet, subnet, NSG, public IP, NIC) ───────────────────────────────
module "network" {
  source = "./modules/network"

  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  name_suffix         = local.name_suffix
  admin_source_cidr   = var.admin_source_cidr
  tags                = local.common_tags
}

# ── VM ────────────────────────────────────────────────────────────────────────
module "vm" {
  source = "./modules/vm"

  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  name_suffix         = local.name_suffix
  vm_size             = local.effective_vm_size
  admin_username      = var.admin_username
  ssh_public_key      = var.ssh_public_key
  nic_id              = module.network.nic_id
  # Render cloud-init template and base64-encode inline — no extra provider needed.
  custom_data = base64encode(templatefile("${path.module}/cloud-init.tftpl", {
    demo              = var.demo
    bp_opamp_endpoint = var.bp_opamp_endpoint
    bp_secret_key     = var.bp_secret_key
    admin_username    = var.admin_username
  }))
  tags = local.common_tags
}
