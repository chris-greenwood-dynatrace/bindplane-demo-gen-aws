output "public_ip" {
  description = "Public IPv4 address (Elastic IP) of the demo instance."
  value       = module.vm.public_ip
}

output "ssh_command" {
  description = "Ready-to-paste SSH command to reach the instance."
  value       = "ssh ${var.admin_username}@${module.vm.public_ip}"
}

output "vm_name" {
  description = "Name tag of the EC2 instance."
  value       = module.vm.vm_name
}

output "instance_id" {
  description = "ID of the EC2 instance. All run resources share the owner/demo tags; `terraform destroy` tears them down atomically."
  value       = module.vm.instance_id
}

output "demo" {
  description = "The demo that was deployed."
  value       = var.demo
}

output "admin_username" {
  description = "Admin username on the demo instance (used by SSH)."
  value       = var.admin_username
}
