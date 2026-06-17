variable "demo" {
  type        = string
  description = "Name of the demo to deploy. Must match a directory under ../demos/ that contains a manifest.yaml."

  validation {
    condition     = contains([for f in fileset("${path.module}/../demos", "*/manifest.yaml") : dirname(f)], var.demo)
    error_message = "demo must be one of the directories under demos/ that contains a manifest.yaml. Valid values: ${join(", ", [for f in fileset("${path.module}/../demos", "*/manifest.yaml") : dirname(f)])}."
  }
}

variable "region" {
  type        = string
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type. Overridden by manifest instance_type_hint if present. Default is cost-optimised for demos."
  default     = "t3.medium"
}

variable "admin_username" {
  type        = string
  description = "Linux admin username on the instance. Should match the AMI's default cloud-init user (Ubuntu = ubuntu)."
  default     = "ubuntu"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key (full openssh-format string) for instance admin access. Key-auth only — password auth is disabled on Ubuntu AMIs by default."
}

variable "admin_source_cidr" {
  type        = string
  description = <<-EOT
    CIDR allowed to reach TCP/22 on the instance. Defaults to 0.0.0.0/0 (any) which is convenient for
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

variable "owner" {
  type        = string
  description = <<-EOT
    Per-operator identifier woven into every AWS resource name and tagged on every
    resource. Lets multiple operators run demos in the same AWS account without
    colliding on VPC / instance / security-group names. Must be 1-12 lowercase alphanumeric
    characters. scripts/up.sh derives this automatically from $OWNER_TAG (or `whoami`
    if unset); override by setting OWNER_TAG in .env.
  EOT

  validation {
    condition     = can(regex("^[a-z0-9]{1,12}$", var.owner))
    error_message = "owner must be 1-12 lowercase alphanumeric characters (a-z, 0-9). Set OWNER_TAG in .env to override."
  }
}
