#!/usr/bin/env bash
# (4a) Install the KubeRay operator via Helm, pinned to the small default pool.
#      The operator only manages RayCluster CRs; the actual Ray GPU *workers*
#      land on kuberay-gpu-pool via the RayCluster spec (see
#      manifests/raycluster-sample.yaml).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd helm
require_cmd kubectl

log "Adding KubeRay Helm repo..."
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ >/dev/null 2>&1 || true
helm repo update kuberay >/dev/null

log "Installing/upgrading kuberay-operator v${KUBERAY_VERSION} into namespace '${KUBERAY_NAMESPACE}' (pinned to ${DEFAULT_POOL_NAME})..."
# helm upgrade --install is idempotent. nodeSelector keeps the operator on the
# default pool (it has no tolerations, so it cannot land on the tainted pools).
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "$KUBERAY_VERSION" \
  --namespace "$KUBERAY_NAMESPACE" --create-namespace \
  --values "${SCRIPT_DIR}/manifests/kuberay-operator-values.yaml" \
  --set-string nodeSelector."cloud\.google\.com/gke-nodepool"="$DEFAULT_POOL_NAME"

log "Waiting for the operator to become available..."
kubectl -n "$KUBERAY_NAMESPACE" rollout status deploy/kuberay-operator --timeout=180s

ok "KubeRay operator installed."
kubectl -n "$KUBERAY_NAMESPACE" get pods -o wide
log "RayCluster CRD:"
kubectl get crd rayclusters.ray.io 2>/dev/null && ok "rayclusters.ray.io present" || warn "RayCluster CRD not found (operator may still be starting)."
