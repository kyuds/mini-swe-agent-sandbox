# mini-swe-agent-sandbox

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) RL fine-tuning with
[SkyRL](https://github.com/NovaSky-AI/SkyRL) on the
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) backend (GKE) —
each SWE-bench box runs as an isolated (gVisor) `Sandbox` pod instead of a local Podman container.

**Standalone:** `pip install` this repo (it depends on `skyrl`), then run the entrypoint here. SkyRL is
a library dependency; nothing in the SkyRL repo is modified. The SkyRL `mini_swe_agent` *example* code
(entrypoint / generator / utils) isn't shipped in the `skyrl` wheel, so it's **copied into this repo
and edited** to use the agent-sandbox backend.

## Layout

| Path | What |
|------|------|
| `mini_swe_agent_sandbox/environment.py` | **The backend.** `AgentSandboxEnvironment` — creates a `Sandbox` CR per trajectory and `kubectl exec`s into it. |
| `mini_swe_agent_sandbox/main.py` | Entrypoint (`python -m mini_swe_agent_sandbox.main`). Copy of SkyRL's `main_mini_swe.py`. |
| `mini_swe_agent_sandbox/generator.py` | `MiniSweAgentGenerator` + the `init_and_run` Ray task. Copy of SkyRL's `mini_swe_generator.py`. |
| `mini_swe_agent_sandbox/utils.py` | `get_sb_environment` / eval. Copy of SkyRL's `mini_swe_utils.py` **+ one branch** injecting the per-instance image for our backend. |
| `mini_swe_agent_sandbox/preprocess.py` | SWE-Gym dataset prep. Copy of SkyRL's `preprocess_swegym.py`. |
| `configs/` | `swebench_agent_sandbox.yaml` (task config wired to the backend) + `litellm.json`. |
| `.env.miniswe` | `OPENAI_BASE_URL` (inference endpoint) + `LITELLM_MODEL_REGISTRY_PATH`. |
| `scripts/run_mini_swe_agent_sandbox.sh` | One-command launcher (`uv run ... -m mini_swe_agent_sandbox.main`). |
| `infra/` | GKE cluster, KubeRay, agent-sandbox controller, gVisor sandbox pool, RBAC (`up.sh` / `teardown.sh`). |
| `docs/` | `port-plan.md` (design + self-review) and `port-guided.md` (implementation walkthrough). |

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

## Status
Backend + wiring implemented and statically validated (compile; import of the backend against real
`kubernetes`; unit-tested exit-code/lifecycle logic). **Not yet run on a cluster.** `uv lock` for the
`fsdp` stack must be run on the target (linux/GPU) — see `pyproject.toml [tool.uv]` (mirrors SkyRL's
resolution) and `docs/port-guided.md` §6–§7 for what's validated and the cluster-dependent assumptions
to confirm (Sandbox readiness, pod-exec exit codes, TTL field, and that the installed `skyrl` version
provides the `skyrl.train.*` / `skyrl.backends.skyrl_train.*` modules the copied code imports).
