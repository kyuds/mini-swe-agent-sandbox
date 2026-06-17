#!/usr/bin/env bash
# Sanity-check the whole stack without launching any real (GPU) workload.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd kubectl

log "=== Nodes by pool / gVisor runtime ==="
kubectl get nodes -L cloud.google.com/gke-nodepool,sandbox.gke.io/runtime

log "=== gVisor RuntimeClass (created automatically by the gVisor pool) ==="
kubectl get runtimeclass gvisor 2>/dev/null && ok "RuntimeClass 'gvisor' present" \
  || warn "RuntimeClass 'gvisor' missing — is the sandbox-gvisor-pool up?"

log "=== CRDs ==="
kubectl get crd 2>/dev/null | grep -E 'ray\.io|agents\.x-k8s\.io' || warn "expected CRDs not found."

log "=== Operators ==="
kubectl -n "$KUBERAY_NAMESPACE" get pods 2>/dev/null || warn "kuberay operator namespace missing."
kubectl -n "$AGENT_SANDBOX_NAMESPACE" get pods 2>/dev/null || warn "agent-sandbox namespace missing."

log "=== RBAC (auth can-i as the runner SA) ==="
subj="system:serviceaccount:${RAY_NAMESPACE}:${SANDBOX_RUNNER_SA}"
for check in \
  "create sandboxes.agents.x-k8s.io|${SANDBOX_NAMESPACE}" \
  "delete sandboxes.agents.x-k8s.io|${SANDBOX_NAMESPACE}" \
  "create pods/exec|${SANDBOX_NAMESPACE}" \
  "get pods|${SANDBOX_NAMESPACE}"; do
  verb_res="${check%|*}"; ns="${check#*|}"
  if kubectl auth can-i $verb_res --as="$subj" -n "$ns" >/dev/null 2>&1; then
    ok "  ALLOW  $verb_res  (ns=$ns)"
  else
    warn "  DENY   $verb_res  (ns=$ns)"
  fi
done

echo
log "To validate the gVisor pool cheaply (no GPU needed), apply the example cold sandbox:"
log "  kubectl apply -f ${SCRIPT_DIR}/manifests/sandbox-example.yaml"
log "  kubectl -n ${SANDBOX_NAMESPACE} get pods -w        # wait for Running"
log "  kubectl -n ${SANDBOX_NAMESPACE} exec deploy/... -- dmesg | head   # gVisor reports a synthetic kernel"
ok "verification complete."
