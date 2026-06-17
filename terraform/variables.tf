variable "demo" {
  type        = string
  description = "Name of the demo to deploy. Must match a directory under ../demos/ that contains a manifest.yaml."

  validation {
    condition     = contains([for f in fileset("${path.module}/../demos", "*/manifest.yaml") : dirname(f)], var.demo)
    error_message = "demo must be one of the directories under demos/ that contains a manifest.yaml. Valid values: ${join(", ", [for f in fileset("${path.module}/../demos", "*/manifest.yaml") : dirname(f)])}."
  }
}

variable "location" {
  type        = string
  description = "Azure region for all resources."
  default     = "eastus"
}

variable "vm_size" {
  type        = string
  description = "Azure VM SKU. Overridden by manifest vm_size_hint if present. Default is cost-optimised for demos."
  default     = "Standard_B2s"
}

variable "admin_username" {
  type        = string
  description = "Linux admin username on the VM."
  default     = "azureuser"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key (full openssh-format string) for VM admin access. Key-auth only — password auth is disabled."
}

variable "admin_source_cidr" {
  type        = string
  description = <<-EOT
    CIDR allowed to reach TCP/22 on the VM. Defaults to 0.0.0.0/0 (any) which is convenient for
    ephemeral demo environments. For production or shared environments narrow this to your IP:
    e.g. "203.0.113.10/32". scripts/up.sh will auto-detect and set this to your current public IP
    if ADMIN_SOURCE_CIDR is left blank in .env.
  EOT
  default     = "0.0.0.0/0"
}

variable "bp_opamp_endpoint" {
  type        = string
  description = "BindPlane Cloud OpAMP WebSocket endpoint, e.g. wss://app.bindplane.com/v1/opamp."
  sensitive   = true
}

variable "bp_secret_key" {
  type        = string
  description = "BindPlane Cloud secret key for OpAMP enrollment."
  sensitive   = true
}

variable "resource_prefix" {
  type        = string
  description = "Short prefix prepended to all resource names. Change to avoid naming collisions when running multiple demos simultaneously."
  default     = "bpdemo"
}
