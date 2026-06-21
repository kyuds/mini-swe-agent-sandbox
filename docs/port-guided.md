# Guided walkthrough: porting `mini_swe_agent` from Podman to agent-sandbox

> **RELOCATED + UPDATED (now in the `mini-swe-agent-sandbox` repo).** The file table below described
> files placed inside SkyRL; they now live here and SkyRL is untouched. Mapping:
> `agent_sandbox_env.py` → `mini_swe_agent_sandbox/environment.py`;
> `swebench_agent_sandbox.yaml` → `configs/`; `run_*.sh` → `scripts/`. **SkyRL is now a pip dependency
> (`skyrl[fsdp]`), not patched** — its example code is copied in and edited: `main_mini_swe.py` →
> `mini_swe_agent_sandbox/main.py`, `mini_swe_generator.py` → `generator.py`, `mini_swe_utils.py` →
> `utils.py` (with the image-injection `elif`), `preprocess_swegym.py` → `preprocess.py`. Deps live in
> this repo's `pyproject.toml` (`mini-swe-agent<2` + a mirrored SkyRL `[tool.uv]` block for the fsdp
> stack); entrypoint `python -m mini_swe_agent_sandbox.main`. RBAC is `infra/05-setup-rbac.sh`. Code
> deltas vs. this doc: the env now **uses the `k8s-agent-sandbox` SDK** (`K8sHelper` + `constants`) for
> the Sandbox lifecycle, so the GVK is the CRD-correct **`agents.x-k8s.io/v1alpha1`** (verified on the
> live cluster; the earlier v1beta1 was wrong); creation is still a direct CR (per-instance image) and
> exec is still pod-exec (no `:8888`). The pod spec adds **`automountServiceAccountToken: false`** +
> gVisor placement. **The code is also split across two files now:** `environment.py` is the thin
> mini-swe-agent adapter (config schema + the `execute`/`cleanup`/`serialize` protocol), while **all
> Kubernetes + agent-sandbox-SDK plumbing lives in `mini_swe_agent_sandbox/kubernetes_util.py`** as the
> `KubernetesSandbox` class. So the method names in §3.1 below now map as: `_start_sandbox` →
> `KubernetesSandbox.start`, `_pod_exec` → `KubernetesSandbox.exec`, `cleanup` →
> `KubernetesSandbox.delete` (the env's `cleanup()` delegates), and `_resolve_pod_name` /
> `_parse_exec_returncode` / `_lifecycle_spec` moved alongside them. The env exposes read-only
> `sandbox_name` / `pod_name` properties. Also fixed: a `service_account` config field that was
> referenced in the pod spec but never declared (would have crashed `start`).

**What this is:** a review guide to the actual code I wrote to swap the example's SWE-bench sandbox backend
from local Podman to kubernetes-sigs/agent-sandbox. Pairs with the design doc
`skyrl-agent-sandbox-port-plan.md` (rationale + self-evaluation) and the research in
`skyrl-agent-sandbox-deployment-architecture.md`.

**Status:** implemented; **not run on a cluster** (no GPU/k8s env available, per the request). It is
validated by: `py_compile` on 3.11 + 3.12, a real module import against the installed `kubernetes` +
`k8s-agent-sandbox`, and unit tests of the pure logic (exit-code parser, TTL spec, config kwarg-filtering).
What is *not* validated is anything requiring a live cluster (CR reconciliation, pod-exec round-trips) — see
§6/§7.

---

## 1. What changed (and how to run it)

| File | New/edit | Purpose |
|---|---|---|
| `examples/train/mini_swe_agent/agent_sandbox_env.py` | **NEW** (~260 ln) | `AgentSandboxEnvironment` — the drop-in `Environment` |
| `examples/train/mini_swe_agent/mini_swe_utils.py` | **EDIT** (+4 ln) | inject per-instance image for the new backend |
| `examples/train/mini_swe_agent/swebench_agent_sandbox.yaml` | **NEW** (copy) | task config with the agent-sandbox `environment:` block |
| `examples/train/mini_swe_agent/run_mini_swe_agent_sandbox.sh` | **NEW** (copy) | launch script (correct paths, new extra) |
| `examples/train/mini_swe_agent/k8s/rbac.yaml` | **NEW** | namespace + SA + Role/RoleBinding |
| `pyproject.toml` | **EDIT** | pin `mini-swe-agent<2`; add `miniswe-agent-sandbox` extra + conflict entry |

**Untouched:** `mini_swe_generator.py`, the `init_and_run` Ray task, the existing `swebench.yaml`/Podman run
scripts, and all of `skyrl-train`/`skyrl-gym`. The whole swap rides mini-swe-agent's dotted-path env factory.

Run (after prereqs in §5):
```bash
uv lock                                                              # pyproject changed
kubectl apply -f examples/train/mini_swe_agent/k8s/rbac.yaml         # + the agent-sandbox controller/CRDs
bash examples/train/mini_swe_agent/run_mini_swe_agent_sandbox.sh
```

---

## 2. The design in one picture

```
swebench_agent_sandbox.yaml: environment_class = "...agent_sandbox_env.AgentSandboxEnvironment"
        │  (mini-swe-agent get_environment dotted-path fallback — no upstream patch)
        ▼
get_sb_environment()  ── injects per-instance image ──►  AgentSandboxEnvironment(**env_config)
        │                                                    │ __init__: create Sandbox CR (inline image),
        │                                                    │           wait Ready, resolve pod name
   agent loop / eval ── env.execute(cmd) ──► k8s pod-exec into the Sandbox pod ──► {output, returncode}
        │                                                    │ cleanup(): delete the Sandbox CR
        ▼
   (Strategy A: SDK constants only; lifecycle via raw CustomObjectsApi; exec via CoreV1Api pod-exec)
```

Why Strategy A (not the SDK's `SandboxClient`): the SDK's `create_sandbox` only stamps from a *named
`SandboxTemplate`* (no per-instance image) and its `commands.run` talks to an in-image `:8888` server that
SWE-bench images don't ship. Both are dealbreakers for this workload; see plan §1.

---

## 3. File-by-file walkthrough

### 3.1 `environment.py` (adapter) + `kubernetes_util.py` (the K8s/SDK backend) — the core

Mirrors mini-swe-agent 1.x `DockerEnvironment` so it's a true drop-in. The mini-swe-agent surface
(`AgentSandboxEnvironmentConfig`, `execute`, `cleanup`, `serialize`) lives in `environment.py`; the
lifecycle/exec internals it calls (`start` / `exec` / `delete`, formerly `_start_sandbox` /
`_pod_exec` / `cleanup`) live on `KubernetesSandbox` in `kubernetes_util.py`. Key parts and the *why*:

- **`AgentSandboxEnvironmentConfig` (dataclass).** Same knobs the Docker backend reads (`image`, `cwd`, `env`,
  `forward_env`, `timeout`) plus k8s placement (`namespace`, `resources`, `runtime_class_name`,
  `node_selector`, `sandbox_ready_timeout`, `shutdown_after_seconds`). `container_command` defaults to
  `["sleep","infinity"]` — the direct analogue of the Docker backend's `sleep 2h`: the box must idle so we can
  exec into it (a SWE-bench image's default CMD would exit → CrashLoop).
- **`__init__`** filters unknown kwargs before building the config (so docker-only YAML keys like `executable`
  don't crash a plain dataclass — verified: `executable`/`pull_timeout` are dropped harmlessly), requires a
  non-empty `image`, loads in-cluster k8s config (kubeconfig fallback for local dev), then
  `_start_sandbox()`. It sets `self._sandbox_name` *before* waiting for readiness so a never-Ready box is still
  cleaned up.
- **`_start_sandbox`** builds and POSTs the `Sandbox` CR via `CustomObjectsApi.create_namespaced_custom_object`
  (`agents.x-k8s.io/v1alpha1`, plural `sandboxes`) with an inline `podTemplate` carrying the per-instance
  image, `workingDir=cwd`, `env` (baked as container env), `resources`, and optional `runtimeClassName` /
  `nodeSelector`. GVK + the pod-name annotation come from `k8s_agent_sandbox.constants` so we track the
  installed CRD version (this is the one place we *use the SDK*).
- **`_wait_ready`** watches the Sandbox CR for `status.conditions[Ready]==True` with a deadline (mirrors the
  SDK's `K8sHelper.wait_for_sandbox_ready`). A timeout fails the trajectory rather than hanging.
- **`_resolve_pod_name`** reads `metadata.annotations["agents.x-k8s.io/pod-name"]`, falling back to the
  sandbox name (identical to the SDK's `Sandbox.get_pod_name`).
- **`execute(command, cwd="", *, timeout=None)`** — accepts a **str (1.x)** or **`{"command":...}` dict (2.x)**.
  Common path is just `bash -lc <cmd>` (cwd + env already set on the pod), so no fragile `cd`/`export`
  wrapping in the hot path; a prelude is only built for the (unused-here) per-call cwd override or host
  env-forwarding. Runs via `kubernetes.stream.stream(connect_get_namespaced_pod_exec, ...)`, merges
  stdout+stderr (like the Docker backend's `stderr→stdout`), and returns `{"output","returncode"}`.
- **Exit code (the reward-critical bit).** pod-exec doesn't return rc in-band like `docker exec`. `_pod_exec`
  reads the exec **ERROR_CHANNEL** v1.Status and `_parse_exec_returncode` maps it: `Success→0`,
  `ExitCode→n`. A **failed or garbled** status maps to nonzero (never a false 0), so a real eval failure
  can't be misread as "resolved"; an **empty** channel maps to 0 (the quiet-success case the k8s client
  itself assumes — confirm on a real cluster, §7.3). Timeouts/exec-start failures → `returncode=-1`.
  (Unit-tested.)
- **`cleanup()` / `__del__`** delete the Sandbox CR (idempotent, ignore 404, never raise). This replaces the
  Docker backend's fire-and-forget background `docker rm` with an explicit, debuggable delete; the optional
  TTL is a backstop.

### 3.2 `mini_swe_utils.py` — 4-line edit
`get_sb_environment` previously set the per-instance `image` only for `environment_class in {docker,
singularity}`. Added an `elif ...endswith("AgentSandboxEnvironment")` that sets `env_config["image"]`. That's
the *only* generator-side change, and it's additive — the Podman path is byte-for-byte unaffected.

### 3.3 `swebench_agent_sandbox.yaml` — task config
Created by **copying `swebench.yaml`** and replacing only the `environment:` block, so the large agent
prompt/template block (which encodes the `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` submission protocol the
1.x agent relies on) stays identical. New block sets `environment_class` to the dotted path, `namespace`,
`container_name`, `sandbox_ready_timeout: 600` (large images), keeps the same `env:` vars, and ships
commented-out `runtime_class_name`/`node_selector`/`resources`/`shutdown_after_seconds` for cluster tuning.

### 3.4 `run_mini_swe_agent_sandbox.sh` — launch script
Copy of `run_mini_swe_8B.sh` with three changes: `--extra miniswe-agent-sandbox` (k8s deps),
`miniswe_config_path` → the new YAML, and the path is **correct** (`examples/train/mini_swe_agent/...`) —
the original scripts point at a stale `examples/mini_swe_agent/...` that doesn't exist (§8). All
training/inference/placement flags are identical to the 8B Podman run.

### 3.5 `pyproject.toml`
- `miniswe` extra: `mini-swe-agent>=1.12.0` → `>=1.12.0,<2` (the example targets the 1.x API — §8).
- New `miniswe-agent-sandbox` extra = `miniswe`'s deps + `kubernetes>=27.2.0` + `k8s-agent-sandbox>=0.4.6`.
- Added `{ extra = "miniswe-agent-sandbox" }` to the same conflict group as `miniswe` (conflicts with
  megatron/gpu/tpu/flashrl, compatible with `fsdp` — matching how `--extra fsdp --extra miniswe` works).

### 3.6 `k8s/rbac.yaml`
Namespace `skyrl-sandboxes`, SA `skyrl-trainer`, and a Role granting exactly what the env needs:
`sandboxes` create/get/list/watch/delete + `pods` get/list + `pods/exec` create. Comments explain the
cross-namespace binding case. Missing RBAC is the #1 silent failure (403 on create).

---

## 4. Old → new mapping (so the swap is auditable)

| Podman path (`DockerEnvironment`) | agent-sandbox path (`AgentSandboxEnvironment`) |
|---|---|
| `podman run -d --rm -w /testbed <img> sleep 2h` | create `Sandbox` CR, `podTemplate` with `<img>`, `workingDir`, `command:[sleep,infinity]` |
| `-e K=V` per exec | container `env` baked into the pod spec |
| `podman exec -w cwd <id> bash -lc <cmd>` | `connect_get_namespaced_pod_exec` → `bash -lc <cmd>` |
| exit code from `subprocess.returncode` | exit code parsed from exec ERROR_CHANNEL status |
| `__del__` → background `docker rm -f` | `cleanup()`/`__del__` → delete Sandbox CR (+ optional TTL) |
| host CPU, unbounded (Ray `num_cpus=0.01` fiction) | pod `resources.requests`, scheduler-enforced |

---

## 5. Deploy & run (prerequisites, documented not automated)
1. **Cluster:** install the agent-sandbox controller + CRDs (`agents.x-k8s.io/v1alpha1`) — primer §3.
2. **RBAC/ns:** `kubectl apply -f .../k8s/rbac.yaml`; run the trainer pod with `serviceAccountName: skyrl-trainer`.
3. **Deps:** `uv lock` (pyproject changed), then the run script's `uv run ... --extra miniswe-agent-sandbox`.
4. **Data:** `preprocess_swegym.py` as before.
5. **Topology:** lean PoC = single 8×GPU pod + local Ray (no KubeRay); sandboxes on a (optionally gVisor)
   sandbox node pool via `node_selector`/`runtime_class_name` in the YAML. See deployment-architecture doc.

---

## 6. What I validated vs. did NOT test
**Validated:** `py_compile` (3.11 + 3.12); real `import` of the module against installed `kubernetes` +
`k8s-agent-sandbox` (confirms `kubernetes.stream.stream`, `ERROR_CHANNEL==3`, and the SDK GVK/annotation
constants all resolve); unit tests of `_parse_exec_returncode` (incl. never-silently-0), `_lifecycle_spec`,
config defaults, and the kwarg-filter; YAML + `pyproject.toml` parse; black-formatted to the repo's 120 cols.

**NOT tested (needs a live GKE cluster + GPUs):** Sandbox CR reconciliation, pod readiness, pod-exec
round-trips and real exit-code recovery, RBAC sufficiency, image pull behavior, end-to-end rewards.

## 7. Assumptions to verify on a real cluster (all fail-safe by design)
1. **What makes a directly-created `Sandbox` "Ready"** (assumed: pod-Running with no readinessProbe). If the
   controller requires probes/`:8888`, readiness will time out → trajectory fails (loud, not silent).
2. **`spec.lifecycle` on a bare `Sandbox`** (TTL). The SDK only sets it on the *Claim*. TTL is **off by
   default** so we never POST an invalid CR; turn it on only after confirming CRD support.
3. **ERROR_CHANNEL exit-code format** across k8s versions — failed/garbled statuses map to nonzero (worst
   case: a falsely-*unresolved* eval). The one exception is an *empty* channel → 0 (assumes quiet=success, as
   the k8s client does); confirm a given version can't send an empty channel on *failure*, else flip
   empty→nonzero in `_parse_exec_returncode`.
4. **Dotted-path import on the worker** — fine in the single-pod PoC (same package the example already imports).

## 8. Pre-existing issues I found (orthogonal to the port, but they gate "does it run")
1. **Version pin = the big one.** `uv.lock` pins `mini-swe-agent==2.3.1`, but the example's generator targets
   the **1.x** API (`execute(str)`, `run()->tuple`, `get_observation`/`parse_action` hooks). At 2.3.1 it is
   broken regardless of sandbox backend (silent all-zero rewards / mis-unpacked `run()`). **Fix applied:** pin
   `<2` in `pyproject.toml`. This also realigns my env's primary (string) `execute` path. *Verified by
   installing both 1.17.5 and 2.3.1 and diffing the contracts.*
2. **Stale paths** in the original `run_mini_swe_8B/30B.sh` (`miniswe_config_path=examples/mini_swe_agent/...`)
   and `.env.miniswe` (`LITELLM_MODEL_REGISTRY_PATH=examples/mini_swe_agent/litellm.json`) — missing the
   `train/` segment; that dir doesn't exist. My new run script uses correct paths; I left the originals and
   `.env.miniswe` untouched (shared) — recommend fixing them separately.
3. **Python floor.** `mini_swe_utils.py` uses a 3.12-only f-string (nested same quotes) while
   `requires-python>=3.11`; the example effectively needs 3.12. Not mine to fix here, but worth a flag.

## 9. Suggested next steps
- Smoke-test on KinD with a tiny image (`local`/`python-runtime` Sandbox) to validate CR create→ready→
  pod-exec→delete and the ERROR_CHANNEL exit-code path *without* GPUs, before the full GKE run.
- Decide warm-pool / concurrency-cap strategy for the ~128-pod step peak (deployment-architecture §4b).
- Upstream wishlist: a `create_sandbox(..., image=...)` / inline-pod-spec path in the SDK would let us drop
  the raw `CustomObjectsApi` usage.
- Fix the pre-existing stale paths (§8.2) and consider the Python floor (§8.3) in a separate PR.
