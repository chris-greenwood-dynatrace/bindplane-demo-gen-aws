output "public_ip" {
  description = "Public IPv4 address of the demo VM."
  value       = module.network.public_ip_address
}

output "ssh_command" {
  description = "Ready-to-paste SSH command to reach the VM."
  value       = "ssh ${var.admin_username}@${module.network.public_ip_address}"
}

output "vm_name" {
  description = "Name of the Azure Linux VM resource."
  value       = module.vm.vm_name
}

output "resource_group_name" {
  description = "Resource group containing all demo resources. Run `terraform destroy` to tear everything down atomically."
  value       = azurerm_resource_group.this.name
}

output "demo" {
  description = "The demo that was deployed."
  value       = var.demo
}

output "admin_username" {
  description = "Admin username on the demo VM (used by SSH)."
  value       = var.admin_username
}
