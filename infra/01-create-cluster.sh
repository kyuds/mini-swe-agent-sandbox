#!/usr/bin/env bash
# (1) Create the GKE Standard cluster with a small default node pool.
#     VPC-native (--enable-ip-alias) gives flat pod-to-pod networking across all
#     node pools — that is what makes cross-pool, in-cluster traffic "just work".
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd gcloud
require_cmd kubectl
require_project

if cluster_exists; then
  ok "cluster '$CLUSTER_NAME' already exists — skipping creation."
else
  log "Creating GKE Standard cluster '$CLUSTER_NAME' in $ZONE (default pool: ${DEFAULT_POOL_NODES}x ${DEFAULT_POOL_MACHINE})..."

  wi_flag=()
  [ "$ENABLE_WORKLOAD_IDENTITY" = "true" ] && wi_flag=(--workload-pool="${PROJECT_ID}.svc.id.goog")

  gcloud container clusters create "$CLUSTER_NAME" \
    --project "$PROJECT_ID" \
    $(gcloud_location) \
    --release-channel "$RELEASE_CHANNEL" \
    --machine-type "$DEFAULT_POOL_MACHINE" \
    --num-nodes "$DEFAULT_POOL_NODES" \
    --enable-ip-alias \
    --enable-autoupgrade \
    --enable-autorepair \
    --no-enable-basic-auth \
    --addons GcePersistentDiskCsiDriver \
    "${wi_flag[@]}"

  ok "cluster created."
fi

log "Fetching cluster credentials into kubeconfig..."
gcloud container clusters get-credentials "$CLUSTER_NAME" $(gcloud_location) --project "$PROJECT_ID"
ok "kubectl is now pointed at '$CLUSTER_NAME'. Current nodes:"
kubectl get nodes -o wide || true
