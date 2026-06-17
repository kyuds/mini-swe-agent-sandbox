#!/usr/bin/env bash
# (2)+(3) Add the two specialized node pools to the cluster:
#   - kuberay-gpu-pool : GPU nodes, tainted so only Ray GPU workers schedule here
#   - sandbox-gvisor-pool : gVisor (GKE Sandbox) nodes for untrusted sandbox pods
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd gcloud
require_project
cluster_exists || die "cluster '$CLUSTER_NAME' does not exist — run 01-create-cluster.sh first."

# --- (2) KubeRay GPU pool ---------------------------------------------------
if [ "${SKIP_GPU_POOL:-0}" = "1" ]; then
  warn "SKIP_GPU_POOL=1 — not creating '$GPU_POOL_NAME' (build sandbox path without GPU quota)."
elif nodepool_exists "$GPU_POOL_NAME"; then
  ok "node pool '$GPU_POOL_NAME' already exists — skipping."
else
  log "Creating GPU pool '$GPU_POOL_NAME' (${GPU_MACHINE}, ${GPU_PER_NODE}x ${GPU_TYPE}, autoscale ${GPU_POOL_MIN}-${GPU_POOL_MAX})..."
  log "  NOTE: this requires GPU quota for '${GPU_TYPE}' in ${REGION}; without it the call fails."
  gcloud container node-pools create "$GPU_POOL_NAME" \
    --cluster "$CLUSTER_NAME" --project "$PROJECT_ID" $(gcloud_location) \
    --machine-type "$GPU_MACHINE" \
    --accelerator "type=${GPU_TYPE},count=${GPU_PER_NODE},gpu-driver-version=${GPU_DRIVER_VERSION}" \
    --image-type cos_containerd \
    --num-nodes "$GPU_POOL_MIN" \
    --enable-autoscaling --min-nodes "$GPU_POOL_MIN" --max-nodes "$GPU_POOL_MAX" \
    --node-labels "$GPU_POOL_LABEL" \
    --node-taints "$GPU_POOL_TAINT" \
    --enable-autoupgrade --enable-autorepair
  ok "GPU pool created."
fi

# --- (3) gVisor sandbox pool ------------------------------------------------
if nodepool_exists "$SANDBOX_POOL_NAME"; then
  ok "node pool '$SANDBOX_POOL_NAME' already exists — skipping."
else
  log "Creating gVisor sandbox pool '$SANDBOX_POOL_NAME' (${SANDBOX_MACHINE}, autoscale ${SANDBOX_POOL_MIN}-${SANDBOX_POOL_MAX})..."
  # --sandbox type=gvisor makes GKE add: the 'gvisor' RuntimeClass, the node
  # label sandbox.gke.io/runtime=gvisor, and the taint
  # sandbox.gke.io/runtime=gvisor:NoSchedule. cos_containerd is required.
  gcloud container node-pools create "$SANDBOX_POOL_NAME" \
    --cluster "$CLUSTER_NAME" --project "$PROJECT_ID" $(gcloud_location) \
    --machine-type "$SANDBOX_MACHINE" \
    --image-type cos_containerd \
    --sandbox type=gvisor \
    --num-nodes "$SANDBOX_POOL_MIN" \
    --enable-autoscaling --min-nodes "$SANDBOX_POOL_MIN" --max-nodes "$SANDBOX_POOL_MAX" \
    --node-labels "$SANDBOX_POOL_LABEL" \
    --enable-autoupgrade --enable-autorepair
  ok "gVisor sandbox pool created."
fi

log "Node pools now in cluster:"
gcloud container node-pools list --cluster "$CLUSTER_NAME" $(gcloud_location) --project "$PROJECT_ID"
log "Nodes by pool / gVisor runtime:"
kubectl get nodes -L cloud.google.com/gke-nodepool,sandbox.gke.io/runtime 2>/dev/null || true
