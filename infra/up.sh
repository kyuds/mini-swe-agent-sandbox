#!/usr/bin/env bash
# Convenience orchestrator: runs the numbered steps in order.
# COSTS MONEY (GPU + gVisor nodes). Re-runnable; each step is idempotent.
#
#   ASSUME_YES=1 ./up.sh     # skip the confirmation prompt
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

cat <<EOF
About to build GKE infrastructure:
  project        : ${PROJECT_ID:-<unset!>}
  location       : ${ZONE}
  cluster        : ${CLUSTER_NAME}
  default pool   : ${DEFAULT_POOL_NODES}x ${DEFAULT_POOL_MACHINE}
  GPU pool       : ${GPU_MACHINE} (${GPU_PER_NODE}x ${GPU_TYPE}), autoscale ${GPU_POOL_MIN}-${GPU_POOL_MAX}
  sandbox pool   : ${SANDBOX_MACHINE} (gVisor), autoscale ${SANDBOX_POOL_MIN}-${SANDBOX_POOL_MAX}
  operators      : kuberay-operator v${KUBERAY_VERSION}, agent-sandbox ${AGENT_SANDBOX_VERSION}
  raycluster     : DEPLOY_RAYCLUSTER=${DEPLOY_RAYCLUSTER:-0} (1 = also apply the sample RayCluster)

This provisions billable resources (GPUs require quota).
EOF
confirm "Proceed?" || die "aborted."

bash "${SCRIPT_DIR}/00-check-prerequisites.sh"
bash "${SCRIPT_DIR}/01-create-cluster.sh"
bash "${SCRIPT_DIR}/02-create-nodepools.sh"
bash "${SCRIPT_DIR}/03-install-kuberay.sh"
bash "${SCRIPT_DIR}/04-install-agent-sandbox.sh"
bash "${SCRIPT_DIR}/05-setup-rbac.sh"
bash "${SCRIPT_DIR}/06-verify.sh"

if [ "${DEPLOY_RAYCLUSTER:-0}" = "1" ]; then  # change to 1 to activate
  bash "${SCRIPT_DIR}/07-deploy-raycluster.sh"
else
  log "Skipping RayCluster (set DEPLOY_RAYCLUSTER=1 to apply manifests/raycluster-sample.yaml)."
fi

ok "Infrastructure ready."
