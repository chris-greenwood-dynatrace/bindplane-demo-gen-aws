# terraform/

Demo-agnostic Azure root module. Provisions ONE ephemeral Ubuntu 22.04 VM + single resource group per demo run.

## Usage

`scripts/up.sh --demo <name>` drives this entirely — it sources `.env`, generates `secrets.auto.tfvars`, runs `terraform apply -var demo=<name>`, waits for cloud-init, then `rsync`s the demo directory and starts `docker compose`. Run `scripts/down.sh` to drain collectors and run `terraform destroy`.

Manual apply: `terraform -chdir=terraform apply -var-file=secrets.auto.tfvars -var demo=manufacturing`.

All resources are in one resource group (`rg-bpdemo-<demo>`) so `terraform destroy` is a single atomic teardown.
