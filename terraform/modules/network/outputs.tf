output "subnet_id" {
  description = "ID of the public subnet to launch the instance in."
  value       = aws_subnet.this.id
}

output "security_group_id" {
  description = "ID of the security group to attach to the instance."
  value       = aws_security_group.this.id
}

output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}
