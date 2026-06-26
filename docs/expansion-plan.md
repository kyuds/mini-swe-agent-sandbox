# skyrl-sandbox expansion plan (generation test + multiplication example + restructure)

Plan + decisions for three pieces of feedback. Companion to `docs/agent-sandbox-research.md` (the
agent-sandbox research, requirement 3); the mini-swe-agent backend's design lives in the
`skyrl_sandbox/mini_swe_agent/` module docstrings. **Status: design + initial implementation; nothing
run on a cluster.**
Caveats are consolidated at the end.

The repo was renamed **`mini-swe-agent-sandbox` → `skyrl-sandbox`** (it now hosts more than one
example). The Python package is renamed to `skyrl_sandbox` with one subpackage per example.

---

## 0. Repo restructure (foundation for everything below)

**Target layout** (one folder per example, shared infra):

```
skyrl-sandbox/
  skyrl_sandbox/                     # top-level package (was: mini_swe_agent_sandbox/)
    mini_swe_agent/                  # example 1 — SWE-bench, per-instance images, pod-exec
      environment.py, kubernetes_util.py, generator.py, main.py,
      generate.py (NEW), utils.py, preprocess.py, __init__.py
    multiplication/                  # example 2 — toy task, ONE image, SDK commands.run
      sandbox.py, env.py, dataset.py, main.py, generate.py, __init__.py (NEW)
  configs/
    mini_swe_agent/  swebench_agent_sandbox.yaml, litellm.json
    multiplication/  multiply_sandbox.yaml
  scripts/
    mini_swe_agent/  run_*.sh, smoke_test_agent_sandbox.py, run_smoke_in_pod.sh
    multiplication/  run_*.sh
  infra/                             # SHARED across both examples (see §3)
    manifests/  sandbox-example.yaml, sandbox-template-multiplication.yaml (NEW), ...
  docs/
```

**Decision — dotted paths change.** `environment_class` becomes
`skyrl_sandbox.mini_swe_agent.environment.AgentSandboxEnvironment`. We own every config that
references it, so this is a mechanical update. (Caveat: any external notes pointing at the old path
are stale.)

**Why a package + subpackages** (not two top-level packages): the two examples share the security
posture (gVisor placement, no API token) and the agent-sandbox SDK; a common parent leaves room for a
future `skyrl_sandbox/common/` without another rename.

---

## 1. mini-swe-agent generation test against an OpenAI endpoint (requirement 1)

**Goal:** run *only* the SkyRL generator (rollouts, no training/weight updates) for the mini-swe-agent
example, with the LLM served by a **remote OpenAI endpoint** (your token), exercising the
agent-sandbox backend for the agent's bash.

### Research answers that drive the design (full detail from the SkyRL source)
- **Generate-only is supported.** Pattern: `skyrl/train/entrypoints/main_generate.py`
  (`EvalOnlyEntrypoint` overrides `get_train_dataset()->None`, builds only the inference client +
  generator, calls `evaluate(...)` — no trainer/optimizer/weight-sync). Harbor has the same shape
  (`main_harbor_generate.py`). **No mini-swe generate-only entrypoint ships**, so we add one.
- **Ray is required — but only in-process `ray.init()` (local mode), NOT a KubeRay cluster.** Every
  entrypoint does `initialize_ray(cfg)` + `ray.get(<@ray.remote entrypoint>.remote(cfg))`, and
  `MiniSweAgentGenerator` dispatches each trajectory as `init_and_run.remote()`
  (`@ray.remote(num_cpus=0.01)`). A single-process Ray on one CPU box satisfies all of this.
- **GPUs are NOT required for generation.** `MiniSweAgentGenerator` calls the LLM via
  litellm → `OPENAI_BASE_URL` (`litellm_model_name = "openai/" + model.path`), bypassing SkyRL's
  inference engine for the actual call. Point `OPENAI_BASE_URL` at a remote endpoint and there is no
  local vLLM/GPU. (Set `generator.inference_engine.run_engines_locally=false` and
  `trainer.placement.colocate_all=false` so no GPU placement group is built.)

### Decision on `up-smoke.sh`
**Do NOT modify it.** Your conditional was: *if generation needs GPUs, add a CPU Ray node pool; if it
doesn't, leave up-smoke as-is.* Generation does **not** need GPUs, and the Ray it needs is in-process
local mode (runs fine on the existing CPU nodes / the smoke-runner pod). So up-smoke stays. (If you
later want the *full* KubeRay-operator path, that already exists in `up.sh`, not up-smoke.)

### Implementation: `skyrl_sandbox/mini_swe_agent/generate.py`
A generate-only entrypoint = `EvalOnlyEntrypoint`'s skeleton + `MiniSWEPPOExp`'s `get_generator`:
- `MiniSWEGenerateExp(BasePPOExp)`: `get_train_dataset()->None`; `get_generator()` builds
  `MiniSweAgentGenerator(...)`; `run(client)` wakes the (remote) inference client, builds the eval
  dataloader, calls `evaluate(...)`, logs metrics.
- `main()`: `MiniSWEConfig.from_cli_overrides`, `validate`, `initialize_ray`,
  `ray.get(eval_entrypoint.remote(cfg))`.
- Launch script `scripts/mini_swe_agent/run_generate_fireworks.sh` (Qwen via Fireworks) uses litellm's
  **native `fireworks_ai` provider** — `FIREWORKS_AI_API_KEY` only, no `OPENAI_*` hijack. CLI overrides set
  `trainer.policy.model.path=Qwen/Qwen3-4B` (tokenizer), `generator.miniswe_litellm_model_name=fireworks_ai/accounts/fireworks/models/<id>`
  (the **full provider-qualified litellm model id**, used verbatim — see below), `run_engines_locally=false`,
  `colocate_all=false`, the eval dataset path, and a tiny batch. The generator decouples the litellm
  model id from `model.path`: the override is used verbatim with its provider prefix
  (`fireworks_ai/…`, `openai/gpt-4o-mini`, …), and an empty override falls back to `openai/<model.path>`
  (the SkyRL local-vLLM training path, which is legitimately OpenAI-compatible). The tokenizer is a
  stand-in for the served model (fine for generation, not training). litellm.json is only an optional
  cost/context registry — providers are built into litellm and selected by the prefix, so nothing needs
  adding there to use Fireworks.

**This still requires** the agent-sandbox cluster (the agent's bash runs in a Sandbox pod via our
backend) + a preprocessed eval dataset. It is a real end-to-end generation, not a unit test.

---

## 2. Multiplication example on agent-sandbox (requirement 2)

**Goal:** a second example that (a) needs only **one** image, (b) spawns from an agent-sandbox
**SandboxTemplate**, (c) uses the **agent-sandbox Python SDK** (`create_sandbox` + `commands.run`) to
execute and read results — i.e. the *opposite* end of the design space from mini-swe (per-instance
images + raw CR + pod-exec).

### What "the multiplication example" actually is upstream
`examples/train/multiply/` — `MultiplyEnv(BaseTextEnv)`: prompt is `"{a} * {b}"`, model answers in
`\boxed{...}`, reward (1.0 correct / 0.5 boxed-but-wrong / 0.0 none) is computed **in pure Python, no
sandbox/image**. Dataset is synthetic parquet (`multiply_dataset.py`). It trains with the **default**
SkyRL generator (no custom generator). So upstream multiply uses *no* image — "one image" is the
target we're *adding* by routing its execution through a sandbox.

### The harbor tension — decision (judgment call; you wanted harbor "tried")
You asked to use **harbor** as the harness. Research finding: harbor is an external framework whose
**execution backend is internal** (chosen by `environment.type` ∈ docker/gke/daytona/modal/e2b/…),
reached behind `Trial.run()`. SkyRL integrates harbor purely at the **`GeneratorInterface`** seam
(`HarborGenerator`/`HarborExp`). To run agent-sandbox *through* harbor we'd have to write a new harbor
**environment provider** inside the external harbor package (source not available locally, pinned git
rev, Python ≥3.12, needs provider keys). That's a large, mostly-out-of-our-repo effort and does **not**
exercise the agent-sandbox SDK (harbor would own execution).

**Decision:** implement the multiplication example as a **SkyRL-gym `BaseTextEnv` whose execution goes
through the agent-sandbox SDK** (`create_sandbox(warmpool=…)` + `commands.run`), driven by SkyRL's
**default** generator. This is the path that actually exercises agent-sandbox + the SDK (the whole
point), is entirely in-repo, and is the simplest thing that can run. It mirrors harbor's *architecture*
(plug in at SkyRL's env/generator seam) without depending on harbor's internal provider API.

**Harbor, kept as a documented follow-up (so it can still be "tried"):** we scaffold a harbor
**task directory** for multiply (`instruction.md` + `environment/Dockerfile` + `tests/test.sh`) under
`examples/`-style layout, and document running it through *real* harbor with harbor's own `docker`/`gke`
provider (`uv run --extra harbor ...`). That is the faithful "use harbor" path; it just doesn't use
the agent-sandbox SDK. See §2 caveats.

### Implementation
- `skyrl_sandbox/multiplication/sandbox.py` — thin SDK wrapper: picks the connection config
  (`SandboxInClusterConnectionConfig(use_pod_ip=True)` when running in-cluster as a Ray worker;
  `SandboxLocalTunnelConnectionConfig` for a laptop), `SandboxClient.create_sandbox(warmpool=…)` (0.5.x:
  claim → SandboxWarmPool → SandboxTemplate),
  `commands.run(cmd, timeout)->ExecutionResult(stdout,stderr,exit_code)`, `delete_sandbox`. **All SDK,
  no pod-exec.**
- `skyrl_sandbox/multiplication/env.py` — `MultiplySandboxEnv(BaseTextEnv)`: ports MultiplyEnv's
  task/reward, but the correctness check **runs inside the sandbox** (`commands.run("python3 -c
  'print(<a>*<b>)'")`, compare to the model's boxed answer). Sandbox created on first use, deleted on
  `close()`. Demonstrates single-image + SDK exec with a trivial task.
- `skyrl_sandbox/multiplication/dataset.py` — synthetic multiply parquet (port of
  `multiply_dataset.py`), `env_class="multiply_sandbox"`.
- `skyrl_sandbox/multiplication/main.py` — training entrypoint: `register(id="multiply_sandbox",
  entry_point="skyrl_sandbox.multiplication.env:MultiplySandboxEnv")` inside the Ray task, then
  `BasePPOExp` (mirrors `main_multiply.py`).
- `skyrl_sandbox/multiplication/generate.py` — generate-only variant (default generator + the env).
- `configs/multiplication/multiply_sandbox.yaml` + `scripts/multiplication/run_*.sh`.
- `infra/manifests/sandbox-template-multiplication.yaml` — the **SandboxTemplate** (single image =
  the agent-sandbox runtime image that ships the `:8888` server; gVisor; no API token) +
  `infra/manifests/sandbox-warmpool-multiplication.yaml` — the **SandboxWarmPool** that references it
  (0.5.x requires a pool for SDK create; `replicas` = warm sandboxes kept ready).

---

## 3. Infra refactor to support both examples

Finding: the infra is **already example-agnostic** (cluster, gVisor pool, controller, RBAC, KubeRay
are generic). The only mini-swe-specific bit is `infra/manifests/sandbox-example.yaml` (a `python:3.11`
cold Sandbox). Changes:
- Keep `sandbox-example.yaml` as the generic cold-Sandbox smoke artifact (rename comment to drop the
  SWE-bench framing).
- Add `infra/manifests/sandbox-template-multiplication.yaml` + `sandbox-warmpool-multiplication.yaml`
  (a `SandboxTemplate` + `SandboxWarmPool`, need the extensions CRDs — installed by default,
  `INSTALL_AGENT_SANDBOX_EXTENSIONS=true`).
- No change to up.sh/up-smoke.sh/RBAC (the runner SA already covers sandboxes + pods/exec; for the
  SDK-`commands.run` path the runner additionally needs network egress to the pod `:8888`, which is
  in-cluster pod-to-pod traffic — allowed by default; flag if a NetworkPolicy is later added).

---

## 4. agent-sandbox research (requirement 3)
See **`docs/agent-sandbox-research.md`**. One-liners: (1) 0.5.x's SDK `create_sandbox(warmpool=…)`
*requires* a warm pool (→ template); template/pool-less only via a raw `Sandbox` CR (what mini-swe
does), losing `commands.run`. (2) Warm pools are **single-image** (one `sandboxTemplateRef` per pool) —
useless for SWE-bench's per-instance images, and the *required* spawn unit for the single-image
multiplication example.

---

## 5. Consolidated caveats (for review)
1. **Runtime image (blocker for `commands.run`) — RESOLVED: there is no published default; you build
   it.** The image that serves the `:8888` API `commands.run` POSTs to is a reference FastAPI server
   (`POST /execute -> {stdout, stderr, exit_code}`) built from agent-sandbox's
   `examples/python-runtime-sandbox/` (`FROM python:3.14-slim`). Upstream templates use
   `IMAGE_PLACEHOLDER` / `sandbox-runtime:latest` (a locally-built tag), so our SandboxTemplate keeps
   `image: IMAGE_PLACEHOLDER` with the full build+push recipe in its header
   (`infra/manifests/sandbox-template-multiplication.yaml`). Build from that example (or any image
   serving the same `/execute` contract on `:8888`), push to a registry the cluster can pull, and set
   `image:`. Our `MultiplicationSandbox.run` already maps that exact `stdout/stderr/exit_code` contract.
   (mini-swe is unaffected — it pod-execs, no `:8888`.)
2. **Generate-only for mini-swe is untested-by-example.** SkyRL ships no mini-swe generate-only
   entrypoint; ours mirrors `main_generate.py` + `MiniSWEPPOExp`. The remote-endpoint +
   `run_engines_locally=false` + `colocate_all=false` combo isn't exercised by SkyRL's scripts —
   verify no code path still forces a GPU placement group. A HF tokenizer for `model.path` still loads
   (CPU only).
3. **litellm model id is decoupled from the tokenizer + uses native providers.** `trainer.policy.model.path`
   (default `Qwen/Qwen3-4B`) loads only the tokenizer (any valid HF id with a chat template); the model
   actually called is `generator.miniswe_litellm_model_name`, used **verbatim** with its provider prefix
   — e.g. `fireworks_ai/accounts/fireworks/models/qwen3-4b` (auth `FIREWORKS_AI_API_KEY`; **verify the
   slug in your Fireworks catalog**) or `openai/gpt-4o-mini` (auth `OPENAI_API_KEY`). Empty override =
   `openai/<model.path>` (SkyRL local vLLM). For a hosted model the tokenizer is a *stand-in* (fine for
   generation/eval token bookkeeping, not training). litellm.json is just an optional cost/context
   registry — you do **not** add providers there; they're built into litellm and chosen by the prefix.
4. **harbor not literally used.** We mirror harbor's GeneratorInterface architecture and scaffold a
   harbor task-dir, but do not run the external harbor framework (it owns execution internally and
   would need a custom agent-sandbox provider; Python ≥3.12 + provider keys). The harbor-native path
   is documented as a follow-up.
5. **Multiplication task is contrived.** Reward verification runs in the sandbox to exercise
   `commands.run`; the task itself doesn't *need* a sandbox. A tool-use variant (model issues shell
   commands, env runs them) is a natural, more realistic extension.
6. **Warm pool is required (0.5.x) and wired.** `create_sandbox(warmpool=…)` needs a `SandboxWarmPool`
   — shipped as `infra/manifests/sandbox-warmpool-multiplication.yaml` (`replicas: 2`, referencing the
   template). Size `replicas` to peak concurrent trajectories; its v1beta1 fields mirror the upstream
   `extensions/examples/` (confirm against your installed `extensions.yaml`).
7. **Nothing run on a cluster / no OpenAI call made here.** Static checks only (compile/import).
   Dotted-path renames updated in our configs + smoke test; external references to old paths are stale.
8. **SDK ↔ controller version coupling (0.5.x / v1beta1).** Pinned `k8s-agent-sandbox>=0.5.0,<0.6` (lock
   the minor, patch floats; venv verified: 0.5.0 → `v1beta1`, `create_sandbox(warmpool=…)`). The
   mini-swe backend reads GVK from the SDK `constants` so it auto-tracks, but the cluster's
   `AGENT_SANDBOX_VERSION` controller must serve **v1beta1** (true for `latest`; do **not** pin it to a
   v1alpha1-era release).
9. **Two LLM backends (the end goal): generation via Fireworks, training via your own vLLM.** Both run
   through the same code via `generator.miniswe_litellm_model_name`: empty → `openai/<model.path>` →
   `OPENAI_BASE_URL` = SkyRL's in-process **vLLM** (H100s, OpenAI-compatible) for *training* (litellm
   correctly tokenizes the real model); `fireworks_ai/…` → Fireworks for the no-GPU *generation* demo.
   Keep `OPENAI_BASE_URL` (it's the training mechanism, not a hack). **Caveat — multiplication differs:**
   it uses SkyRL's **default** generator (`RemoteInferenceEngine`), which posts to
   `generator.inference_engine.remote_urls` and sends **no auth header** — so it can only hit a no-auth
   vLLM (your own), **not** Fireworks/OpenAI. mini-swe (litellm) gets the hosted-provider path;
   multiplication's demo is training (local vLLM) or generation against your own vLLM.
