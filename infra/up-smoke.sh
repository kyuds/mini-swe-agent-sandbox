#!/usr/bin/env bash
# Minimal bring-up for the agent-sandbox SMOKE TEST (CPU only): cluster + agent-sandbox controller + RBAC.
# NO GPU pool, NO KubeRay, NO gVisor pool, NO RayCluster — just enough to prove the AgentSandboxEnvironment
# works end-to-end *as the runner ServiceAccount* (so RBAC is exercised). Cheap (one CPU default node).
# Re-runnable; each step is idempotent. Mirrors up.sh but trimmed.
#
#   ASSUME_YES=1 ./up-smoke.sh     # skip the confirmation prompt
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

cat <<EOF
About to build a MINIMAL (CPU-only) GKE setup for smoke-testing agent-sandbox:
  project        : ${PROJECT_ID:-<unset!>}
  location       : ${ZONE}
  cluster        : ${CLUSTER_NAME}
  default pool   : ${DEFAULT_POOL_NODES}x ${DEFAULT_POOL_MACHINE}
  gVisor pool    : ${SANDBOX_MACHINE} (gVisor), autoscale ${SANDBOX_POOL_MIN}-${SANDBOX_POOL_MAX}
  agent-sandbox  : ${AGENT_SANDBOX_VERSION}
  RBAC           : ns ${RAY_NAMESPACE} + ns ${SANDBOX_NAMESPACE}, SA ${SANDBOX_RUNNER_SA}
  SKIPPED        : GPU pool, KubeRay, RayCluster

Cheaper than ./up.sh (no GPUs). Still billable (control plane + CPU default node + gVisor pool node).
EOF
confirm "Proceed?" || die "aborted."

bash "${SCRIPT_DIR}/00-check-prerequisites.sh"
bash "${SCRIPT_DIR}/01-create-cluster.sh"
# gVisor sandbox pool only (SKIP_GPU_POOL=1) — for realistic isolation.
SKIP_GPU_POOL=1 bash "${SCRIPT_DIR}/02-create-nodepools.sh"
bash "${SCRIPT_DIR}/04-install-agent-sandbox.sh"
bash "${SCRIPT_DIR}/05-setup-rbac.sh"

ok "Smoke-test cluster ready: CPU cluster + gVisor sandbox pool + agent-sandbox controller + RBAC."
log "Now run the smoke test FROM AN IN-CLUSTER POD (as ${SANDBOX_RUNNER_SA}, so RBAC is tested):"
log "  bash ${SCRIPT_DIR}/../scripts/run_smoke_in_pod.sh        # gVisor ON by default (matches this pool)"
log "Tear down when done:  ASSUME_YES=1 ${SCRIPT_DIR}/teardown-smoke.sh"
log "See docs/testing-agent-sandbox.md for the manual steps."
