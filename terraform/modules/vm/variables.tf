variable "name_suffix" {
  type        = string
  description = "Suffix appended to the instance name (e.g. bpdemo-manufacturing)."
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type."
}

variable "admin_username" {
  type        = string
  description = "Linux admin username (the AMI's default cloud-init user)."
}

variable "ssh_public_key" {
  type        = string
  description = "Full openssh-format public key string for admin SSH access."
}

variable "subnet_id" {
  type        = string
  description = "ID of the subnet to launch the instance in."
}

variable "security_group_id" {
  type        = string
  description = "ID of the security group to attach to the instance."
}

variable "user_data" {
  type        = string
  description = "Cloud-init payload (plain text; AWS base64-encodes it)."
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the instance and its root volume."
  default     = {}
}
