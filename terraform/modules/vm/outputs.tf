output "vm_name" {
  description = "Name tag of the EC2 instance."
  value       = aws_instance.this.tags["Name"]
}

output "instance_id" {
  description = "ID of the EC2 instance."
  value       = aws_instance.this.id
}

output "public_ip" {
  description = "Elastic IP address attached to the instance."
  value       = aws_eip.this.public_ip
}
