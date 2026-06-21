# Port plan: mini_swe_agent sandbox backend, Podman → agent-sandbox

> **RELOCATED + UPDATED (now lives in the `mini-swe-agent-sandbox` repo).** This doc was written when
> the code lived inside the SkyRL tree. The decisions below stand, with these deltas applied in the
> repo: (1) **standalone — `skyrl` is a pip dependency and the SkyRL repo is NOT modified.** SkyRL's
> example code (`main`/`generator`/`utils`/`preprocess`) is **copied into the `mini_swe_agent_sandbox/`
> package and edited**; the only diff vs the upstream example is a one-line image-injection branch in
> `utils.py` for our backend (+ `mini-swe-agent<2` pinned in `pyproject.toml`). Entrypoint: `python -m
> mini_swe_agent_sandbox.main`. (2) the Sandbox
> GVK is **`agents.x-k8s.io/v1beta1`** (matching `infra/manifests/sandbox-example.yaml`), and we
> **dropped the `k8s-agent-sandbox` SDK dep** (its constants say v1alpha1 and lag the deployed CRD);
> (3) the pod spec now sets **`automountServiceAccountToken: false`** + gVisor `runtimeClassName`/
> nodeSelector/tolerations per the repo's example; (4) **RBAC/namespaces are handled by
> `infra/05-setup-rbac.sh`** (`skyrl-sandboxes` ns, `skyrl-sandbox-runner` SA), so no separate
> `k8s/rbac.yaml`. Broader research docs (`skyrl-agent-sandbox-{deployment-architecture,…}.md`) remain
> in `~/dev/skyrl-plan/`.

**Goal:** make the `examples/train/mini_swe_agent` example run its SWE-bench rollout/eval sandboxes as
**kubernetes-sigs/agent-sandbox** pods instead of local Podman containers, with **zero changes to
skyrl-train/skyrl-gym** and minimal changes to the example. PoC quality; **not run/tested** (no GPU cluster) —
this doc + the guided doc record design + every assumption to verify.

Companion docs (`~/dev/skyrl-plan/`): `skyrl-mini-swe-agent-integration-points.md` (the seam),
`agent-sandbox-primer.md` (agent-sandbox reference), `skyrl-agent-sandbox-deployment-architecture.md` (runtime
+ GKE topology). Guided implementation writeup: `skyrl-agent-sandbox-port-guided.md`.

---

## 1. Research findings that pin the design (all verified against installed packages)

| Fact | Source | Consequence |
|---|---|---|
| `mini-swe-agent` **1.17.5** `DockerEnvironment.execute(command: str, ...)` → `{"output","returncode"}`; `run() -> tuple`; agent has `get_observation/parse_action/execute_action/render_template/add_message`, `model.n_calls` | `/tmp/msw1x` (installed 1.17.5) | The SkyRL adapter is written for **1.x** and matches it exactly. |
| `mini-swe-agent` **2.3.1** `execute(action: dict, ...)`, `run() -> dict`, none of those agent hooks exist | `/tmp/msw231` + Env Protocol `minisweagent/__init__.py:66` | At the **locked** 2.3.1 the example is broken (string-vs-dict, tuple-vs-dict unpack, dead hooks). |
| `uv.lock` pins `mini-swe-agent==2.3.1` (floor `>=1.12.0`) | `uv.lock:4061` | **Root cause** of the example being broken — a dep bump crossed the 1.x→2.x break. |
| `get_environment()` resolves `environment_class` via registry **+ dotted-path fallback** in both 1.x and 2.x | `environments/__init__.py:28` (1.17.5) | A new backend plugs in by class path; no upstream patch. |
| agent-sandbox SDK **`k8s-agent-sandbox==0.4.6`**: `SANDBOX_API_GROUP="agents.x-k8s.io"`, `version="v1alpha1"`, plural `sandboxes`; pod-name annotation `agents.x-k8s.io/pod-name` | `/tmp/asb/.../constants.py` | GVK is **v1alpha1** (primer guessed v1beta1 — corrected). |
| SDK `create_sandbox(template, ...)` creates a **SandboxClaim** from a named SandboxTemplate; **no image override** | `sandbox_client.py:94`, `k8s_helper.py:43` | Can't use the SDK's high-level create for SWE-bench's per-instance images. |
| SDK `commands.run` POSTs `{"command":...}` to an **`:8888` HTTP server** in the image | `commands/command_executor.py:30` | Strategy B needs that server; SWE-bench images don't have it. |
| Sandbox readiness: watch CR `status.conditions[Ready]=True`; pod name from CR annotation | `k8s_helper.py:131`, `sandbox.py:81` | Gives us a tested readiness/resolve pattern to mirror. |

**Decision (the one real fork): Strategy A.** Create the `Sandbox` CR **directly** (inline `podTemplate` with
the per-instance image), and run commands with the **k8s pod-exec API** (the direct analogue of `docker exec`).
Use the SDK only for **GVK/annotation constants** (version-alignment); bypass `SandboxClient`/`SandboxClaim`
(template-only) and `commands.run` (`:8888`). Rationale: keeps SWE-bench images unmodified, maps 1:1 onto the
`execute(cmd)->{output,returncode}` contract, isolates all new code to one ~250-line `Environment` class.

---

## 2. Design: `AgentSandboxEnvironment` (drop-in for `DockerEnvironment`)

Implements the mini-swe-agent `Environment` contract so `get_environment` picks it up via
`environment_class: "examples.train.mini_swe_agent.agent_sandbox_env.AgentSandboxEnvironment"`.

- **`__init__(**kwargs)`** → build a config dataclass (filtering unknown keys, like the primer sketch);
  load in-cluster k8s config (fallback kubeconfig); create `CustomObjectsApi` + `CoreV1Api`; **create the
  Sandbox CR** (`_create_sandbox`); **wait Ready** (`_wait_ready`, mirrors `K8sHelper.wait_for_sandbox_ready`);
  **resolve pod name** from the `agents.x-k8s.io/pod-name` annotation (fallback = sandbox name).
- **`_create_sandbox`** posts:
  ```yaml
  apiVersion: agents.x-k8s.io/v1alpha1
  kind: Sandbox
  metadata: {name: skyrl-swe-<uuid8>, namespace: <ns>, labels: {app: skyrl-miniswe, ...}}
  spec:
    podTemplate:
      spec:
        containers:
        - name: <container_name>
          image: <per-instance SWE-bench image>
          command: ["sleep", "infinity"]      # keep the box idle to exec into (≙ DockerEnvironment `sleep 2h`)
          workingDir: <cwd>
          resources: {requests: {...}, limits: {...}}   # configurable
        # optional: runtimeClassName: gvisor; nodeSelector; tolerations
  ```
- **`execute(command, cwd="", *, timeout=None) -> {"output","returncode"}`** — accepts **str (1.x) or dict
  (2.x, `action.get("command")`)**; folds `cwd` + `env` exports into one `bash -lc` script; runs via
  `kubernetes.stream.stream(core.connect_get_namespaced_pod_exec, ...)`; merges stdout+stderr; reads the
  **ERROR_CHANNEL** to recover the exit code (the part `docker exec` gives for free); on timeout/exception
  returns `returncode=-1`.
- **`cleanup()` / `__del__`** — delete the Sandbox CR (idempotent, ignore 404), guarded by a flag; the heavy
  cleanup the Podman path did via background `docker rm` becomes an explicit CR delete + optional TTL.
- **`get_template_vars()` / `serialize()`** — minimal, for agent template rendering + trajectory save.

---

## 3. File-level change plan (all additive except 2 surgical edits)

| File | Change | Why |
|---|---|---|
| `examples/train/mini_swe_agent/agent_sandbox_env.py` | **NEW** — the `Environment` class | the port |
| `examples/train/mini_swe_agent/mini_swe_utils.py` | **EDIT** `get_sb_environment` — inject per-instance `image` for the agent-sandbox class | the factory only set `image` for `docker`/`singularity` |
| `examples/train/mini_swe_agent/swebench_agent_sandbox.yaml` | **NEW** — copy of `swebench.yaml` with the agent-sandbox `environment:` block | keep the Podman path intact; opt-in |
| `examples/train/mini_swe_agent/run_mini_swe_agent_sandbox.sh` | **NEW** — copy of the 8B run script pointing at the new yaml (+ fixes the stale `examples/mini_swe_agent` paths) | launch the ported variant |
| `examples/train/mini_swe_agent/k8s/{rbac,sandbox-namespace}.yaml` | **NEW** — RBAC (SA → sandboxes + pods/exec) + namespace | the #1 silent failure if missing |
| `pyproject.toml` | **EDIT** — pin `mini-swe-agent>=1.12.0,<2`; add `kubernetes` + `k8s-agent-sandbox` to a new `miniswe-agent-sandbox` extra | unbreak the example (§1) + the env's deps |

**Not touched:** `mini_swe_generator.py`, anything in `skyrl-train`/`skyrl-gym`, the existing `swebench.yaml`
and Podman run scripts.

---

## 4. Self-evaluation — inconsistencies, risks, and resolutions

Walking the plan adversarially before implementing:

1. **Version contract mismatch (string vs dict).** *Resolved:* `execute` accepts both; primary target is 1.x
   (string) because the recommended fix is pinning `<2`. Under 1.x, the example's `evaluate_trajectory`
   string calls are *correct* — so I must **not** "fix" them to dicts (that was a wrong idea premised on 2.3.1).
2. **The example is broken at the locked 2.3.1 regardless of sandbox backend** (run()-tuple-unpack, agent
   hooks). *Resolved/escalated:* this is **orthogonal** to the sandbox swap and can't be fixed by the env. The
   plan pins `<2`, which realigns the whole adapter. Flagged as a hard prerequisite; not silently worked around.
3. **SDK can't set a per-instance image.** *Resolved:* create the `Sandbox` CR directly; don't use
   `create_sandbox`. (This is why the design uses raw `CustomObjectsApi`, not `SandboxClient`.)
4. **Exit code over pod-exec.** *Risk:* unlike `docker exec`, pod-exec doesn't return rc in-band; the whole
   reward depends on it. *Resolved:* parse the exec **ERROR_CHANNEL** status JSON; default a missing/garbled
   status to a *failure* (`-1`/`1`), never a false `0`, so eval can't be silently marked resolved.
5. **Pod must stay alive to exec into.** *Risk:* a SWE-bench image's default CMD may exit → CrashLoop. *Resolved:*
   set container `command: ["sleep","infinity"]` (≙ Podman path's `sleep 2h`).
6. **What makes a directly-created Sandbox "Ready"?** *Unverified:* assuming pod-Running ⇒ Sandbox Ready when no
   readinessProbe is set. Flagged to verify against the installed controller; readiness watch has a timeout so a
   never-Ready sandbox fails the trajectory rather than hanging forever.
7. **TTL on a direct Sandbox.** *Unverified:* the SDK sets `spec.lifecycle` on the *Claim*, not a bare Sandbox.
   *Resolved:* TTL is **off by default** (so we never post an invalid CR); explicit `cleanup()` is the primary
   reaper; TTL is an opt-in flagged "verify CRD support."
8. **Two sandboxes/trajectory + lifetime overlap → ~128 concurrent pods/step** (from the runtime analysis).
   *Resolved (documented):* sandbox node-pool capacity / `SandboxWarmPool` admission control is a deploy-time
   concern, not a code one; cleanup is explicit and idempotent so a crashed Ray task can't silently leak.
9. **Image pulls dominate.** *Documented:* per-instance images defeat generic warm pools; mirror to Artifact
   Registry / enable Image Streaming. Not a code change.
10. **Dotted-path import on Ray workers.** *Risk:* `examples...agent_sandbox_env` must be importable where
    `init_and_run` runs. *Resolved:* same package the example already imports from; works in the single-pod PoC.
11. **`evaluate_trajectory` runs `git apply`/eval as separate `execute` calls** with a fresh sandbox — must work
    over pod-exec with the long (3600s) eval timeout. *Resolved:* `execute` honors per-call `timeout`; the eval
    box is a second `_create_sandbox` exactly as the Podman path makes a second container.

**Net:** the sandbox swap itself is low-risk and well-isolated. The two genuine "unknowns" (#6 Sandbox-Ready
semantics, #7 TTL field) are isolated, fail-safe, and flagged. The biggest *gotcha* is #2 — which is a
pre-existing breakage the port inherits and the plan fixes via the version pin.

---

## 5. Deploy prerequisites (documented, not automated)
- `kubectl apply` the agent-sandbox controller + CRDs (primer §3), `v1alpha1`.
- Create the sandbox namespace + RBAC (training SA → `sandboxes` create/get/list/watch/delete + `pods`,`pods/exec`).
- Run `uv lock` after the `pyproject.toml` pin/extra change (I do **not** run it here).
- Single-pod PoC (no KubeRay) per the deployment-architecture doc; sandboxes on a (optionally gVisor) pool.
