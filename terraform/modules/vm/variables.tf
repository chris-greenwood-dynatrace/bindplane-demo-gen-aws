variable "resource_group_name" {
  type        = string
  description = "Resource group in which to create the VM."
}

variable "location" {
  type        = string
  description = "Azure region."
}

variable "name_suffix" {
  type        = string
  description = "Suffix appended to the VM name (e.g. bpdemo-manufacturing)."
}

variable "vm_size" {
  type        = string
  description = "Azure VM SKU."
}

variable "admin_username" {
  type        = string
  description = "Linux admin username."
}

variable "ssh_public_key" {
  type        = string
  description = "Full openssh-format public key string for admin SSH access."
}

variable "nic_id" {
  type        = string
  description = "Resource ID of the network interface to attach."
}

variable "custom_data" {
  type        = string
  description = "Base64-encoded cloud-init payload."
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the VM and its OS disk."
  default     = {}
}
