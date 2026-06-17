# terraform/

Demo-agnostic AWS root module. Provisions ONE ephemeral Ubuntu 22.04 EC2 instance (in its own VPC) per demo run.

## Usage

`scripts/up.sh --demo <name>` drives this entirely — it sources `.env`, generates `secrets.auto.tfvars`, runs `terraform apply -var demo=<name>`, waits for cloud-init, then `rsync`s the demo directory and starts `docker compose`. Run `scripts/down.sh` to drain collectors and run `terraform destroy`.

Requires AWS credentials in the environment (e.g. `AWS_PROFILE`, `aws configure`, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) and the target `region` (default `us-east-1`).

Manual apply: `terraform -chdir=terraform apply -var-file=secrets.auto.tfvars -var demo=manufacturing`.

There is no resource group in AWS — every resource carries `owner` + `demo` tags and lives in a dedicated VPC, so `terraform destroy` is a single atomic teardown of the run's state.
