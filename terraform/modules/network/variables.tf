variable "resource_group_name" {
  type        = string
  description = "Name of the resource group in which to create network resources."
}

variable "location" {
  type        = string
  description = "Azure region."
}

variable "name_suffix" {
  type        = string
  description = "Suffix appended to all network resource names (e.g. bpdemo-manufacturing)."
}

variable "admin_source_cidr" {
  type        = string
  description = "CIDR permitted inbound on TCP/22. Use a /32 for your workstation in non-demo environments."
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to all resources in this module."
  default     = {}
}
