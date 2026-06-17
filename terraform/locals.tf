locals {
  # Read the selected demo's manifest — the single source of truth for per-demo settings.
  manifest = yamldecode(file("${path.module}/../demos/${var.demo}/manifest.yaml"))

  # Human-readable display name from the manifest.
  display_name = local.manifest.display_name

  # Total collector count; used in tags for visibility.
  collector_total = local.manifest.collectors.total

  # Allow the manifest to carry an optional vm_size_hint; fall back to the Terraform variable.
  effective_vm_size = try(local.manifest.vm_size_hint, var.vm_size)

  # Stable name suffix derived from the demo slug (safe for Azure resource names).
  name_suffix = "${var.resource_prefix}-${var.demo}"

  # Resource group name — everything lives here so `terraform destroy` is atomic.
  resource_group_name = "rg-${local.name_suffix}"

  # Common tags applied to every resource.
  common_tags = {
    demo            = var.demo
    display_name    = local.display_name
    managed_by      = "terraform"
    collector_total = tostring(local.collector_total)
  }
}
