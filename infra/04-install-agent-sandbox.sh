#!/usr/bin/env bash
# (4b) Install the agent-sandbox controller + CRDs.
#      The controller is lightweight and lands on the default pool (the GPU and
#      gVisor pools are tainted, so it cannot schedule there). We install the
#      core 'Sandbox' CRD always; extensions (Template/Claim/WarmPool) are
#      optional and not needed for cold sandboxes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd kubectl
require_cmd curl

# Resolve the release tag.
if [ "$AGENT_SANDBOX_VERSION" = "latest" ]; then
  require_cmd jq
  log "Resolving latest agent-sandbox release tag..."
  AGENT_SANDBOX_VERSION="$(curl -fsSL https://api.github.com/repos/kubernetes-sigs/agent-sandbox/releases/latest | jq -r '.tag_name')"
  [ -n "$AGENT_SANDBOX_VERSION" ] && [ "$AGENT_SANDBOX_VERSION" != "null" ] \
    || die "could not resolve latest release tag (GitHub API rate limit? set AGENT_SANDBOX_VERSION=vX.Y.Z explicitly)."
fi
base="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}"
log "Installing agent-sandbox ${AGENT_SANDBOX_VERSION}"

log "Applying core controller + Sandbox CRD (manifest.yaml)..."
kubectl apply --server-side -f "${base}/manifest.yaml"

if [ "$INSTALL_AGENT_SANDBOX_EXTENSIONS" = "true" ]; then
  log "Applying extensions CRDs (SandboxTemplate/Claim/WarmPool)..."
  kubectl apply --server-side -f "${base}/extensions.yaml"
else
  warn "Skipping extensions.yaml (INSTALL_AGENT_SANDBOX_EXTENSIONS=false). Cold sandboxes don't need it."
fi

log "Waiting for the controller deployment(s) in '${AGENT_SANDBOX_NAMESPACE}'..."
kubectl -n "$AGENT_SANDBOX_NAMESPACE" wait --for=condition=Available deploy --all --timeout=180s \
  || warn "controller not Available yet — check 'kubectl -n ${AGENT_SANDBOX_NAMESPACE} get pods'."

ok "agent-sandbox installed."
kubectl -n "$AGENT_SANDBOX_NAMESPACE" get pods -o wide 2>/dev/null || true
log "Sandbox CRDs:"
kubectl get crd 2>/dev/null | grep -E 'agents\.x-k8s\.io' || warn "no agents.x-k8s.io CRDs found yet."
