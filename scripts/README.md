# scripts/ — Demo Operator Scripts

All scripts source `scripts/lib/common.sh`. Require: `yq` (brew install yq / snap install yq),
`terraform`, `azure-cli` (logged in), `ssh`, `rsync` (optional, falls back to scp).

## Usage table

| Command | Description |
|---|---|
| `scripts/demos.sh list` | List all available demos with collector count |
| `scripts/up.sh [--demo NAME]` | Spin up a demo on Azure (prompts to pick if --demo omitted) |
| `scripts/up.sh --demo NAME --skip-validate` | Skip static validation (not recommended) |
| `scripts/down.sh [--demo NAME]` | Drain collectors + destroy Azure infra |
| `scripts/ssh.sh` | SSH into the running demo VM |
| `scripts/ssh.sh -L 8080:localhost:8080` | SSH with port-forward |
| `scripts/logs.sh --demo NAME` | Tail all docker compose logs from VM |
| `scripts/logs.sh --demo NAME gateway` | Tail logs for the gateway service only |
| `scripts/validate.sh NAME` | Static validation of a demo (8 checks) before spin-up |
| `scripts/select.sh` | Interactive picker (used internally by up.sh) |

## Quick start

```bash
cp .env.example .env
# fill in BP_SECRET_KEY, DT_ENV_ID, DT_API_TOKEN in .env

scripts/up.sh                  # pick demo interactively
# — or —
scripts/up.sh --demo manufacturing
```

After spin-up, follow `demos/<name>/bindplane/rollout.md` to wire up pipelines in the BindPlane UI.
Then verify telemetry in Dynatrace.

## Tear down

```bash
scripts/down.sh --demo manufacturing
```

Drains collectors (frees BindPlane cap), then destroys the Azure resource group atomically.
BindPlane server-side Configurations persist — on re-spin, collectors re-enroll and get their
pipelines pushed automatically.

## TF_VAR mapping

| Script variable (from .env) | Terraform variable |
|---|---|
| `BP_OPAMP_ENDPOINT` | `bp_opamp_endpoint` |
| `BP_SECRET_KEY` | `bp_secret_key` |
| `AZURE_LOCATION` | `location` |
| `VM_SIZE` | `vm_size` |
| `SSH_PUBLIC_KEY_PATH` (file content) | `ssh_public_key` |
| `ADMIN_SOURCE_CIDR` (auto-detected if blank) | `admin_source_cidr` |
| `--demo NAME` arg | `demo` |
