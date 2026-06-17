output "nic_id" {
  description = "Resource ID of the network interface to attach to the VM."
  value       = azurerm_network_interface.this.id
}

output "public_ip_address" {
  description = "Public IPv4 address of the VM (static, Standard SKU)."
  value       = azurerm_public_ip.this.ip_address
}
