#!/usr/bin/env bash
# (optional) Deploy a RayCluster onto the GPU pool.
# Gated by DEPLOY_RAYCLUSTER in up.sh; also runnable standalone.
#
# Quota-safe to apply: the worker group starts at 0 replicas (no GPU scheduled
# until SkyRL scales it up); only the head runs, on the default CPU pool.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd kubectl

sample="${SCRIPT_DIR}/manifests/raycluster-sample.yaml"
gen="${SCRIPT_DIR}/manifests/raycluster.generated.yaml"
[ -f "$sample" ] || die "missing $sample"

# Prereqs created by earlier steps.
kubectl get crd rayclusters.ray.io >/dev/null 2>&1 \
  || die "RayCluster CRD not found — run 03-install-kuberay.sh first."
kubectl get ns "$RAY_NAMESPACE" >/dev/null 2>&1 \
  || warn "namespace '${RAY_NAMESPACE}' not found — run 05-setup-rbac.sh first (apply may fail)."

# Render the sample's defaults to the configured namespace + ServiceAccount so it
# matches the RBAC identity created by 05. (Ray version/images/resources stay as
# authored in the sample — edit that file for those.)
log "Rendering RayCluster to ${gen} (namespace=${RAY_NAMESPACE}, sa=${SANDBOX_RUNNER_SA})..."
sed -e "s|^  namespace: skyrl\$|  namespace: ${RAY_NAMESPACE}|" \
    -e "s|serviceAccountName: skyrl-sandbox-runner|serviceAccountName: ${SANDBOX_RUNNER_SA}|g" \
    "$sample" > "$gen"

# The sample pins workers to the default GPU pool label; warn if it was changed.
if [ "${GPU_POOL_LABEL:-}" != "workload=ray-gpu" ]; then
  warn "GPU_POOL_LABEL='${GPU_POOL_LABEL}' but the sample pins workers to 'workload: ray-gpu'."
  warn "  -> edit nodeSelector/tolerations in ${sample} to match your GPU pool label."
fi

log "Applying RayCluster..."
kubectl apply -f "$gen"
ok "RayCluster applied (worker group at 0 replicas — no GPU consumed on apply)."
kubectl -n "$RAY_NAMESPACE" get raycluster,pods 2>/dev/null || true
