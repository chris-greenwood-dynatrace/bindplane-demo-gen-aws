locals {
  # Read the selected demo's manifest — the single source of truth for per-demo settings.
  manifest = yamldecode(file("${path.module}/../demos/${var.demo}/manifest.yaml"))

  # Human-readable display name from the manifest.
  display_name = local.manifest.display_name

  # Total collector count; used in tags for visibility.
  collector_total = local.manifest.collectors.total

  # Allow the manifest to carry an optional instance_type_hint; fall back to the Terraform variable.
  effective_instance_type = try(local.manifest.instance_type_hint, var.instance_type)

  # Stable name suffix derived from the demo slug + per-operator owner tag, so multiple
  # operators can deploy demos side-by-side in the same AWS account without
  # colliding on VPC / instance / security-group names. Example: bpdemo-clintons-energy.
  name_suffix = "${var.resource_prefix}-${var.owner}-${var.demo}"

  # Common tags applied to every resource. Tagging (not a resource group) is how all
  # of a run's resources are grouped in AWS; `terraform destroy` tears them down atomically.
  common_tags = {
    demo            = var.demo
    display_name    = local.display_name
    owner           = var.owner
    managed_by      = "terraform"
    collector_total = tostring(local.collector_total)
  }
}
