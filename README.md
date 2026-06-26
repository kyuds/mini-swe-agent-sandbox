# skyrl-sandbox

Run [SkyRL](https://github.com/NovaSky-AI/SkyRL) RL workloads on the
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) backend (GKE) —
each rollout's sandbox is an isolated (gVisor) `Sandbox` pod instead of a local Podman/Docker
container. SkyRL is a pip dependency; the SkyRL repo is never modified (its example code is copied in
and edited).

Two examples, one per package folder — they sit at **opposite ends** of the agent-sandbox design space:

| | [`skyrl_sandbox/mini_swe_agent`](skyrl_sandbox/mini_swe_agent) | [`skyrl_sandbox/multiplication`](skyrl_sandbox/multiplication) |
|---|---|---|
| task | [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) SWE-bench | toy `a * b` |
| image | **per-instance** (thousands) | **one fixed** image |
| create | raw `Sandbox` CR (no template) | SDK `create_sandbox(warmpool=…)` → pool → template |
| execute | Kubernetes **pod-exec** | SDK **`commands.run`** (in-image `:8888`) |
| needs `:8888` runtime image? | no | **yes** |

The *why* behind the two backends is in [`docs/agent-sandbox-research.md`](docs/agent-sandbox-research.md)
(template-less create + warm-pool research); the full design + caveats are in
[`docs/expansion-plan.md`](docs/expansion-plan.md). The mini-swe backend's design lives in the module
docstrings ([`environment.py`](skyrl_sandbox/mini_swe_agent/environment.py),
[`kubernetes_util.py`](skyrl_sandbox/mini_swe_agent/kubernetes_util.py)).

Across both, the Ray workers (driving the sandboxes) hold the Kubernetes identity/RBAC; the sandbox
pods run untrusted model code with **no** API token and gVisor isolation (`infra/05-setup-rbac.sh`).

## Install

```bash
uv sync                 # light: env backends + dataset prep (any platform)
uv sync --extra fsdp    # full SkyRL training backend (skyrl[fsdp]; linux/GPU)
```

Cluster (shared by both examples): `cd infra && cp .env.example .env && $EDITOR .env` (set
`PROJECT_ID`), then `./up.sh` (GKE + KubeRay + agent-sandbox + gVisor pool + RBAC).

## Example 1 — mini-swe-agent (SWE-bench)

```bash
# data
uv run python -m skyrl_sandbox.mini_swe_agent.preprocess --output_dir ~/data/swe_gym_subset
# train (GPUs)
bash scripts/mini_swe_agent/run_mini_swe_agent_sandbox.sh
# OR generate-only against a remote endpoint (Qwen via Fireworks, litellm native provider; no GPUs):
FIREWORKS_AI_API_KEY=fw-... bash scripts/mini_swe_agent/run_generate_fireworks.sh
```

**Two LLM backends, one generator.** Training serves the policy on **your own vLLM** (H100s) via
SkyRL's in-process inference engine — an OpenAI-compatible HTTP endpoint reached with litellm's
`openai/` provider (`OPENAI_BASE_URL`, see [`.env.miniswe`](.env.miniswe)). The no-GPU generation demo
uses **Fireworks** via litellm's native `fireworks_ai/` provider. The generator switches between them
with `generator.miniswe_litellm_model_name`: empty → `openai/<model.path>` (local vLLM / training);
`fireworks_ai/…` → Fireworks (generation).

Backend selected by `environment_class:
"skyrl_sandbox.mini_swe_agent.environment.AgentSandboxEnvironment"` in
[`configs/mini_swe_agent/swebench_agent_sandbox.yaml`](configs/mini_swe_agent/swebench_agent_sandbox.yaml).
The example targets the mini-swe-agent **1.x** API, so `pyproject.toml` pins `mini-swe-agent<2`.

## Example 2 — multiplication (single image, agent-sandbox SDK)

```bash
# data
uv run python -m skyrl_sandbox.multiplication.dataset --output_dir ~/data/multiply_sandbox
# apply the SandboxTemplate + SandboxWarmPool -- FIRST set the template image to an agent-sandbox :8888
# runtime image (see caveat). 0.5.x spawns via the pool: claim -> SandboxWarmPool -> SandboxTemplate.
kubectl apply -f infra/manifests/sandbox-template-multiplication.yaml
kubectl apply -f infra/manifests/sandbox-warmpool-multiplication.yaml
# full GRPO training (GPUs) -- the simplest agent-sandbox demo for this example (rollouts exercise commands.run):
bash scripts/multiplication/run_multiply_sandbox.sh
# OR generate-only against YOUR OWN vLLM (HOST:PORT, no auth — see note below; not Fireworks):
REMOTE_URL=HOST:8000 bash scripts/multiplication/run_generate_multiply.sh
```

Each trajectory adopts a `Sandbox` from the `multiplication-pool` warm pool (→ `multiplication-template`)
via `create_sandbox(warmpool=…)` and computes/verifies the product with the SDK's `commands.run`.
**LLM path:** unlike mini-swe (which calls litellm and can use Fireworks), multiplication uses SkyRL's
**default** generator → `RemoteInferenceEngine`, which sends no auth header — so it only talks to your
own no-auth vLLM (`remote_urls`), not Fireworks. The natural demo here is **training** (local vLLM);
generation needs your own vLLM endpoint.
**Caveat:** the template image must ship the agent-sandbox `:8888` runtime server (left as a placeholder
in the manifest on purpose — there is no published default; build it from agent-sandbox's
`examples/python-runtime-sandbox/`) — see
[`docs/expansion-plan.md`](docs/expansion-plan.md) §5. *(harbor was researched as the harness; it owns
execution internally and would need a custom agent-sandbox provider, so we mirror its `GeneratorInterface`
architecture instead — see the plan §2.)*

## Testing the agent-sandbox part (no GPUs)

Validate the mini-swe sandbox contract on a cheap **CPU** cluster — no H100s, no SkyRL training:
```bash
cd infra && ASSUME_YES=1 ./up-smoke.sh    # CPU cluster + gVisor sandbox pool + agent-sandbox + RBAC (no GPU/KubeRay)
cd .. && bash scripts/mini_swe_agent/run_smoke_in_pod.sh   # runs IN-CLUSTER as the runner SA (RBAC + gVisor exercised)
# teardown:  ASSUME_YES=1 infra/teardown-smoke.sh
```
`run_smoke_in_pod.sh` runs the test from a pod as `skyrl-sandbox-runner`, so create → `execute()` →
cleanup is driven exactly as a SkyRL Ray worker would. Leave the Sandbox up for inspection with
`SMOKE_ARGS="--keep"`:
```bash
SMOKE_ARGS="--keep" bash scripts/mini_swe_agent/run_smoke_in_pod.sh
kubectl -n skyrl-sandboxes get sandboxes.agents.x-k8s.io -l app=mini-swe-agent-sandbox   # view
kubectl -n skyrl-sandboxes delete sandbox -l app=mini-swe-agent-sandbox                  # clean up
```
The per-phase detail, a laptop-only mode, and a `kubectl` fallback live in the headers of
[`smoke_test_agent_sandbox.py`](scripts/mini_swe_agent/smoke_test_agent_sandbox.py) and
[`run_smoke_in_pod.sh`](scripts/mini_swe_agent/run_smoke_in_pod.sh).
