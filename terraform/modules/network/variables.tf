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
