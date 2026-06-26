#!/usr/bin/env bash
# Run the multiplication GENERATE-only example (Fireworks; NO GPUs, NO training) FROM AN IN-CLUSTER POD,
# as the runner ServiceAccount — the multiplication analogue of
# scripts/mini_swe_agent/run_generate_in_pod.sh. Unlike the mini-swe backend (per-instance image +
# pod-exec), multiplication uses the agent-sandbox SDK: it adopts a Sandbox from a SandboxWarmPool and
# verifies the product via commands.run against the in-image :8888 server. So this script also stands up
# the pool: a ConfigMap-mounted stdlib :8888 server (no Docker build) + SandboxTemplate + SandboxWarmPool.
#
# What it does (idempotent; re-runnable):
#   1. ensure the `fireworks` Secret (from $FIREWORKS_AI_API_KEY if missing)
#   2. ensure the generate-runner pod is up + Ready (reuses infra/manifests/generate-runner.yaml)
#   3. apply the runtime-server ConfigMap + SandboxTemplate + SandboxWarmPool, wait for warm pods Ready
#   4. tar this repo into the pod (preserves the installed .venv)
#   5. in the pod: `uv sync --extra train` -> synthesize a tiny dataset -> run_generate_fireworks.sh
#
# Prereqs: a cluster with agent-sandbox + the extensions CRDs + RBAC up (infra/up.sh or up-smoke.sh) and
# kubectl pointing at it; and a Fireworks key (export FIREWORKS_AI_API_KEY, or pre-create `fireworks`).
#
# Usage:
#   FIREWORKS_AI_API_KEY=fw-... bash scripts/multiplication/run_generate_in_pod.sh
#   FW_MODEL=accounts/fireworks/models/gpt-oss-20b DATA_LIMIT=2 NUM_DIGITS=2 EVAL_BATCH_SIZE=2 \
#     bash scripts/multiplication/run_generate_in_pod.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

source "${REPO_DIR}/infra/load-config.sh" 2>/dev/null || true
RUNNER_NS="${RAY_NAMESPACE:-skyrl}"                  # where the runner pod + SA live
SANDBOX_NS="${SANDBOX_NAMESPACE:-skyrl-sandboxes}"   # where the warm pool + sandboxes live
POD="${RUNNER_POD:-generate-runner}"
SECRET_NAME="${SECRET_NAME:-fireworks}"

# Generation knobs (forwarded into the pod; defaults match the validated mini-swe run).
SKYRL_EXTRA="${SKYRL_EXTRA:-train}"                  # skyrl[skyrl-train] (no vLLM/GPU). 'fsdp' for the full stack.
FW_MODEL="${FW_MODEL:-accounts/fireworks/models/gpt-oss-20b}"   # must exist in your Fireworks catalog
EVAL_BATCH_SIZE="${EVAL_BATCH_SIZE:-2}"
DATA_LIMIT="${DATA_LIMIT:-2}"                        # number of validation problems (synthetic)
NUM_DIGITS="${NUM_DIGITS:-2}"                        # digits per operand
DATA_DIR_IN_POD="${DATA_DIR_IN_POD:-/root/data/multiply_sandbox}"
POOL_WAIT="${POOL_WAIT:-180}"                        # seconds to wait for a warm sandbox pod to be Ready

command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

# 1. Resolve the Fireworks key (host env wins; else read the Secret) and ensure the Secret exists.
FIREWORKS_AI_API_KEY="${FIREWORKS_AI_API_KEY:-${FIREWORKS_API_KEY:-}}"
if [ -n "$FIREWORKS_AI_API_KEY" ]; then
  if ! kubectl -n "$RUNNER_NS" get secret "$SECRET_NAME" >/dev/null 2>&1; then
    echo ">> creating Secret $RUNNER_NS/$SECRET_NAME from FIREWORKS_AI_API_KEY"
    kubectl -n "$RUNNER_NS" create secret generic "$SECRET_NAME" \
      --from-literal=FIREWORKS_AI_API_KEY="$FIREWORKS_AI_API_KEY" >/dev/null
  fi
elif kubectl -n "$RUNNER_NS" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  FIREWORKS_AI_API_KEY="$(kubectl -n "$RUNNER_NS" get secret "$SECRET_NAME" \
    -o go-template='{{index .data "FIREWORKS_AI_API_KEY" | base64decode}}')"
else
  echo "ERROR: no Fireworks key. Export FIREWORKS_AI_API_KEY, or create the '$SECRET_NAME' secret in ns '$RUNNER_NS'." >&2
  exit 1
fi

# 2. Ensure the runner pod (same pod/manifest as the mini-swe example).
if ! kubectl -n "$RUNNER_NS" get pod "$POD" >/dev/null 2>&1; then
  echo ">> applying runner pod ($RUNNER_NS/$POD)..."
  kubectl apply -f "${REPO_DIR}/infra/manifests/generate-runner.yaml"
fi
echo ">> waiting for $RUNNER_NS/$POD to be Ready..."
kubectl -n "$RUNNER_NS" wait --for=condition=Ready "pod/$POD" --timeout=300s

# 3. Stand up the warm pool: runtime-server ConfigMap -> SandboxTemplate -> SandboxWarmPool (order
#    matters; the template mounts the ConfigMap, the pool references the template).
echo ">> applying multiplication sandbox pool (configmap -> template -> warmpool)..."
kubectl apply -f "${REPO_DIR}/infra/manifests/sandbox-runtime-server-configmap.yaml"
kubectl apply -f "${REPO_DIR}/infra/manifests/sandbox-template-multiplication.yaml"
kubectl apply -f "${REPO_DIR}/infra/manifests/sandbox-warmpool-multiplication.yaml"

echo ">> waiting up to ${POOL_WAIT}s for a warm sandbox pod to be Ready in $SANDBOX_NS ..."
_start=$SECONDS
while true; do
  ready=$(kubectl -n "$SANDBOX_NS" get pods --no-headers 2>/dev/null \
    | awk '{print $2}' | awk -F/ '$1==$2 && $1+0>0' | wc -l | tr -d ' ')
  if [ "${ready:-0}" -ge 1 ]; then echo ">> warm pool ready ($ready pod(s))"; break; fi
  if (( SECONDS - _start > POOL_WAIT )); then
    echo "WARN: no Ready sandbox pod after ${POOL_WAIT}s (create_sandbox will still wait per-claim). Current state:"
    kubectl -n "$SANDBOX_NS" get pods 2>&1 | head
    kubectl -n "$SANDBOX_NS" get sandboxwarmpool,sandboxtemplate 2>&1 | head
    break
  fi
  sleep 5
done

# 4. Sync this repo into the pod (preserves the installed .venv).
echo ">> syncing repo -> $POD:/workspace ..."
kubectl -n "$RUNNER_NS" exec "$POD" -- mkdir -p /workspace
tar czf - -C "$REPO_DIR" \
  --exclude='.git' --exclude='infra' --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' . \
  | kubectl -n "$RUNNER_NS" exec -i "$POD" -- tar xzf - -C /workspace

# 5. In the pod: install (cached) -> synthetic dataset -> generate. Key piped over stdin (never in argv).
echo ">> running generate on $POD (model=$FW_MODEL, extra=$SKYRL_EXTRA, batch=$EVAL_BATCH_SIZE, val=$DATA_LIMIT)..."
printf '%s\n' "$FIREWORKS_AI_API_KEY" | kubectl -n "$RUNNER_NS" exec -i "$POD" -- bash -lc "
  set -e
  read -r FW; export FIREWORKS_AI_API_KEY=\"\$FW\"
  cd /workspace
  echo '== [1/3] install (skyrl[$SKYRL_EXTRA]) =='
  uv sync --extra '$SKYRL_EXTRA'
  echo '== [2/3] dataset ($DATA_LIMIT val problems, $NUM_DIGITS-digit) =='
  uv run --no-sync python -m skyrl_sandbox.multiplication.dataset \
    --output_dir '$DATA_DIR_IN_POD' --num_digits '$NUM_DIGITS' --train_size 0 --test_size '$DATA_LIMIT'
  echo '== [3/3] generate (Fireworks: $FW_MODEL) =='
  SKYRL_EXTRA='$SKYRL_EXTRA' FW_MODEL='$FW_MODEL' EVAL_BATCH_SIZE='$EVAL_BATCH_SIZE' DATA_DIR='$DATA_DIR_IN_POD' \
    bash scripts/multiplication/run_generate_fireworks.sh 2>&1 | tee /tmp/generate-multiply.log
"

echo ">> done. Each trajectory adopts a warm sandbox (claim) and deletes it on close (env.close())."
echo "   NOTE: the SandboxWarmPool stays up and keeps \`replicas\` sandboxes WARM by design, so you'll"
echo "         always see ~that many pods until you delete the pool. Tear it down when finished:"
echo "   kubectl delete -f infra/manifests/sandbox-warmpool-multiplication.yaml \\"
echo "                  -f infra/manifests/sandbox-template-multiplication.yaml \\"
echo "                  -f infra/manifests/sandbox-runtime-server-configmap.yaml"
echo "   pool status:  kubectl -n $SANDBOX_NS get sandboxwarmpool,sandboxes.agents.x-k8s.io,pods"
