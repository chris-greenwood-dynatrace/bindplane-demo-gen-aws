# <DEMO_NAME> Demo

<One paragraph describing what this demo shows and why it matters to Dynatrace SEs.
Example: "This demo shows BindPlane managing a fleet of [N] OpenTelemetry collectors
across a [domain], demonstrating [key capabilities]. All three signals (logs, metrics,
traces) flow through the gateway collector into Dynatrace, illustrating [business value].">

## Business variants

| Variant | Relabeling |
|---------|-----------|
| `<variant_1>` | <what changes — just labels/display names> |
| `<variant_2>` | <what changes> |

All variants use the same telemetry shape and BindPlane configuration. Pass
`--demo <DEMO_NAME>` to `scripts/up.sh` and relabel in the BindPlane UI.

## Spin up

```bash
scripts/up.sh --demo <DEMO_NAME>
```

See `bindplane/rollout.md` for the BindPlane UI steps after spin-up.
