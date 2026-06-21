# Testing the agent-sandbox integration (CPU-only, no SkyRL / no GPUs)

**Goal:** prove to the SkyRL team that `AgentSandboxEnvironment` — the seam between mini-swe-agent/SkyRL
and kubernetes-sigs/agent-sandbox — actually works on a real cluster, **without** H100s or the SkyRL
training stack. The env is fully decoupled from SkyRL and GPUs (the LLM lives behind a separate HTTP
endpoint), so a cheap **CPU** GKE cluster exercises the entire create → `execute()` → cleanup contract
SkyRL relies on.

## Why this is sufficient evidence
SkyRL/mini-swe-agent only touch the sandbox three ways (see `port-guided.md`): construct it from the
YAML `environment:` block (via `get_environment`), call `execute(cmd) -> {"output","returncode"}` many
times, and `cleanup()`. None involves a GPU. If those work against agent-sandbox on CPU, the SkyRL ↔
agent-sandbox path works; only the (unchanged) GPU training half remains.

## Minimal cluster (cluster + gVisor pool + agent-sandbox + RBAC; no GPU/KubeRay)
```bash
cd infra && cp .env.example .env && $EDITOR .env   # set PROJECT_ID (defaults are fine otherwise)
ASSUME_YES=1 ./up-smoke.sh        # 01-create-cluster + 02 (gVisor pool only) + 04-install-agent-sandbox + 05-setup-rbac
```
`up-smoke.sh` is a trimmed `up.sh`: a CPU cluster, the **gVisor sandbox node pool** (so sandboxes get
real kernel isolation, as in production), the agent-sandbox controller+CRD, and the RBAC (namespaces
`skyrl` + `skyrl-sandboxes`, ServiceAccount `skyrl-sandbox-runner`). It skips the GPU pool, KubeRay, and
the RayCluster. Tear it all down with `ASSUME_YES=1 ./teardown-smoke.sh` (deletes the whole cluster).

---

## Method 1 — in-cluster runner (recommended; this is the one that tests RBAC)

A laptop run uses your admin kubeconfig and **bypasses RBAC**. To prove the *real* path — a Ray-worker
pod creating sandboxes with its ServiceAccount's in-cluster token — run the test from a pod that runs
as `skyrl-sandbox-runner`. One command does it:

```bash
bash scripts/run_smoke_in_pod.sh
# pass flags through:  SMOKE_ARGS="--gvisor" bash scripts/run_smoke_in_pod.sh
```

It (1) applies `infra/manifests/smoke-runner.yaml` — a pod on the default pool running **as
`skyrl-sandbox-runner`** (a stand-in for a SkyRL Ray worker); (2) tar-syncs this repo into it; (3) runs
`uv sync` + the smoke test there, targeting `--namespace skyrl-sandboxes`. Every Sandbox create/exec/
delete is then authorized by the SA's token → **RBAC is genuinely exercised** (a missing verb in
`05-setup-rbac.sh` shows up as a 403 here).

### The same thing by hand (the "ssh in, rsync, uv" workflow)
```bash
kubectl apply -f infra/manifests/smoke-runner.yaml
kubectl -n skyrl wait --for=condition=Ready pod/smoke-runner --timeout=180s

# sync the repo into the pod (tar over exec — rsync-style, needs only tar in the pod):
tar czf - --exclude='.git' --exclude='infra' --exclude='__pycache__' . \
  | kubectl -n skyrl exec -i smoke-runner -- tar xzf - -C /workspace
#   (real rsync alternative, if the pod has rsync:
#    rsync -av --exclude .git --exclude infra -e 'kubectl -n skyrl exec -i' ./ smoke-runner:/workspace/ )

# "ssh in" and run it as the SA:
kubectl -n skyrl exec -it smoke-runner -- bash
#   inside the pod:
cd /workspace && uv sync && uv run python scripts/smoke_test_agent_sandbox.py --namespace skyrl-sandboxes --gvisor
```
The script prints `PASS`/`FAIL` per check and a final tally (exit 0 = all passed). `run_smoke_in_pod.sh`
runs with **gVisor ON by default** (matching the pool `up-smoke.sh` creates), so the sandbox pods land
on the gVisor node pool with real kernel isolation — the production-faithful path. To use the plain CPU
pool instead, `GVISOR= bash scripts/run_smoke_in_pod.sh` (or drop `--gvisor` in the manual command).

What each phase proves, and the SkyRL behavior it stands in for:

| Phase | Check | Stands in for |
|------|-------|---------------|
| 1 | create `Sandbox` → wait Ready → resolve pod | `get_sb_environment()` per trajectory |
| 2 | `execute()` → `{output, returncode}` | the agent's per-step bash |
| 3 | `true`→0, `false`→1, `exit 7`→7, stderr+`exit 3`→3 | **RL reward** = eval `returncode == 0`; exit-code fidelity over pod-exec |
| 4 | file persists across execs; shell env does **not** | the agent loop ("new subshell each action") |
| 5 | `bash <<'EOF'` heredoc rc; `git apply <<'PATCH'` (if git in image) | `evaluate_trajectory`'s patch-apply + eval-script form |
| 6 | `sleep 10` with `timeout=2` → rc `-1` | per-command timeout (no hang) |
| 7 | `execute({"command": …})` | mini-swe-agent 2.x dict-action compat |
| 8 | `cleanup()` → CR gone | per-trajectory teardown (no leak) |

---

## Method 2 — laptop run (quick; bypasses RBAC)
Fastest "does the env logic work" check; uses your kubeconfig (admin), so it does **not** validate RBAC.
```bash
uv sync
uv run python scripts/smoke_test_agent_sandbox.py --namespace default
```
Same flags: `--image`, `--cwd`, `--gvisor`, `--keep`. (The Sandbox API version isn't a flag — it comes
from the k8s-agent-sandbox SDK, which tracks the installed CRD: `agents.x-k8s.io/v1alpha1`.)

## Method 3 — pure kubectl (eyeball / debug)
```bash
kubectl apply -n default -f - <<'YAML'
apiVersion: agents.x-k8s.io/v1alpha1
kind: Sandbox
metadata: { name: smoke, namespace: default }
spec:
  podTemplate:
    spec:
      automountServiceAccountToken: false
      restartPolicy: OnFailure
      containers:
      - { name: sandbox, image: python:3.11-slim, command: ["sleep","infinity"], workingDir: /tmp }
YAML
kubectl -n default get sandbox smoke -w        # wait Ready=True
POD=$(kubectl -n default get sandbox smoke -o jsonpath='{.metadata.annotations.agents\.x-k8s\.io/pod-name}')
kubectl -n default exec "$POD" -- bash -lc 'echo hi; exit 7'; echo "rc=$?"   # expect rc=7
kubectl -n default delete sandbox smoke
```

## Troubleshooting (the smoke test prints these hints too)
- *create 404 / "could not find"* → controller/CRD missing (`04-install-agent-sandbox.sh`), or the installed CRD's served version differs from the SDK's `SANDBOX_API_VERSION` (install a matching `k8s-agent-sandbox`).
- *403 forbidden* (Method 1) → RBAC gap; check `05-setup-rbac.sh` granted `sandboxes` + `pods/exec` to the SA.
- *never Ready* → `kubectl -n <ns> get pods,sandboxes`; with `--gvisor`, the gVisor pool is missing.

## What "all checks passed" demonstrates
agent-sandbox can create per-trajectory sandboxes, run the agent's bash and the eval script with
**correct exit codes** (the reward signal), keep filesystem state across steps, honor timeouts, and tear
down cleanly — the entire sandbox half of SkyRL's mini_swe_agent loop — and (via Method 1) does so under
the same ServiceAccount + RBAC a real Ray worker would use. The only remaining piece is the GPU policy/
inference half, unchanged from SkyRL's Podman-backed example.
