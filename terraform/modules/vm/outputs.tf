output "vm_name" {
  description = "Name of the Linux VM resource."
  value       = azurerm_linux_virtual_machine.this.name
}

output "admin_username" {
  description = "Admin username configured on the VM."
  value       = azurerm_linux_virtual_machine.this.admin_username
}
