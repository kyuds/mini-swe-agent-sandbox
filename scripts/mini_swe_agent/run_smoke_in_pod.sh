#!/usr/bin/env bash
# Run the agent-sandbox smoke test FROM AN IN-CLUSTER POD, as the runner ServiceAccount — so it
# exercises the real RBAC + in-cluster-token path (a laptop run uses your admin kubeconfig and
# bypasses RBAC). Steps: ensure the runner pod, sync this repo into it, `uv run` the smoke test there.
#
# Prereqs: ./infra/up-smoke.sh has run (cluster + agent-sandbox + RBAC), and kubectl points at it.
#
#   bash scripts/mini_swe_agent/run_smoke_in_pod.sh                  # defaults
#   SMOKE_ARGS="--gvisor" bash scripts/mini_swe_agent/run_smoke_in_pod.sh   # pass flags through to the test
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Namespaces from infra/.env (fall back to the documented defaults).
source "${REPO_DIR}/infra/load-config.sh" 2>/dev/null || true
RUNNER_NS="${RAY_NAMESPACE:-skyrl}"            # where the runner pod + SA live
SANDBOX_NS="${SANDBOX_NAMESPACE:-skyrl-sandboxes}"  # where it creates Sandboxes (RBAC-scoped here)
POD="${RUNNER_POD:-smoke-runner}"
# gVisor ON by default — up-smoke.sh provisions the gVisor pool. Set GVISOR= to disable (plain CPU pool).
GVISOR="${GVISOR:---gvisor}"

command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

# 1. Ensure the runner pod exists and is Ready.
if ! kubectl -n "$RUNNER_NS" get pod "$POD" >/dev/null 2>&1; then
  echo ">> applying runner pod ($RUNNER_NS/$POD)..."
  kubectl apply -f "${REPO_DIR}/infra/manifests/smoke-runner.yaml"
fi
echo ">> waiting for $RUNNER_NS/$POD to be Ready..."
kubectl -n "$RUNNER_NS" wait --for=condition=Ready "pod/$POD" --timeout=180s

# 2. Sync this repo into the pod (tar over `kubectl exec` — an rsync-style one-shot that needs only
#    tar in the pod; excludes .git/infra/venvs). This is the "rsync the package onto the pod" step.
echo ">> syncing repo -> $POD:/workspace ..."
kubectl -n "$RUNNER_NS" exec "$POD" -- mkdir -p /workspace
tar czf - -C "$REPO_DIR" \
  --exclude='.git' --exclude='infra' --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' . \
  | kubectl -n "$RUNNER_NS" exec -i "$POD" -- tar xzf - -C /workspace

# 3. uv sync (core deps only — no GPU/skyrl) + run the smoke test against the sandbox namespace.
#    The pod's in-cluster token (the SA) authorizes every Sandbox create/exec/delete -> RBAC is real.
echo ">> running smoke test on $POD as SA (sandbox namespace=$SANDBOX_NS)..."
kubectl -n "$RUNNER_NS" exec -i "$POD" -- bash -lc "
  set -e
  cd /workspace
  uv sync
  uv run python scripts/mini_swe_agent/smoke_test_agent_sandbox.py --namespace '$SANDBOX_NS' $GVISOR ${SMOKE_ARGS:-}
"
