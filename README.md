# mini-swe-agent-sandbox

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) RL fine-tuning with
[SkyRL](https://github.com/NovaSky-AI/SkyRL) on the
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) backend (GKE) —
each SWE-bench box runs as an isolated (gVisor) `Sandbox` pod instead of a local Podman container.

## What's different from SkyRL's example

Only two things — everything else is the upstream example, verbatim:
1. **The sandbox backend** is `AgentSandboxEnvironment` (this repo) instead of `DockerEnvironment`,
   selected by `environment_class` in `configs/swebench_agent_sandbox.yaml`.
2. **`utils.py: get_sb_environment`** has one extra `elif` that injects the per-instance SWE-bench
   image for our backend (the upstream only does this for `docker`/`singularity`).

> The example targets the mini-swe-agent **1.x** API, so `pyproject.toml` pins `mini-swe-agent<2`
> (2.x is a breaking change that silently zeroes rewards — see `docs/port-plan.md`).

## How it works

```
python -m mini_swe_agent_sandbox.main  (uses SkyRL framework via pip)
   │  generator.miniswe_config_path → configs/swebench_agent_sandbox.yaml
   ▼
mini-swe-agent get_environment (dotted-path) → mini_swe_agent_sandbox.environment.AgentSandboxEnvironment
   │  __init__: create Sandbox CR (per-instance image, gVisor, no API token), wait Ready
   ▼
agent loop / eval → execute(cmd) → k8s pod-exec into the Sandbox pod → {output, returncode}
   │  cleanup(): delete the Sandbox CR
   ▼
agent-sandbox controller + gVisor node pool (from infra/)
```

The Ray workers (driving the sandboxes) hold the Kubernetes identity/RBAC; the sandbox pods run
untrusted model bash with **no** API token and gVisor isolation (`infra/05-setup-rbac.sh`).

## Quick start

```bash
# 1. Stand up the cluster (GKE + KubeRay + agent-sandbox + gVisor pool + RBAC)
cd infra && cp .env.example .env && $EDITOR .env   # set PROJECT_ID etc.
./up.sh && cd ..

# 2. Install deps (uv). Light (env + preprocess, any platform):
uv sync
#    Full training backend (skyrl[fsdp]; linux/GPU):
uv sync --extra fsdp

# 3. Prepare data
uv run python -m mini_swe_agent_sandbox.preprocess --output_dir ~/data/swe_gym_subset

# 4. Train
bash scripts/run_mini_swe_agent_sandbox.sh
```

## Testing the agent-sandbox part (no GPUs)
Validate the whole sandbox contract on a cheap **CPU** cluster — no H100s, no SkyRL training:
```bash
cd infra && ASSUME_YES=1 ./up-smoke.sh    # CPU cluster + gVisor sandbox pool + agent-sandbox + RBAC (no GPU/KubeRay)
cd .. && bash scripts/run_smoke_in_pod.sh # run the test IN-CLUSTER as the runner SA (RBAC + gVisor exercised)
# teardown when done:  ASSUME_YES=1 infra/teardown-smoke.sh
```
`run_smoke_in_pod.sh` spins up a pod running as `skyrl-sandbox-runner`, syncs this repo in, and runs the
test there (gVisor on by default) — so create → `execute()` → cleanup is driven exactly as a SkyRL Ray
worker would (in-cluster token + RBAC, sandboxes on the gVisor pool), minus the GPU/LLM half.

To leave the Sandbox running at the end for manual inspection, pass `--keep` through `SMOKE_ARGS`:
```bash
SMOKE_ARGS="--keep" bash scripts/run_smoke_in_pod.sh
# then, from your laptop (admin kubeconfig), view and clean it up:
kubectl -n skyrl-sandboxes get sandboxes.agents.x-k8s.io -l app=mini-swe-agent-sandbox
kubectl -n skyrl-sandboxes delete sandbox -l app=mini-swe-agent-sandbox   # when done
```
See `docs/testing-agent-sandbox.md` for the manual ssh/rsync steps, a laptop-only run, the phase →
SkyRL-usage map, and a `kubectl` fallback.
