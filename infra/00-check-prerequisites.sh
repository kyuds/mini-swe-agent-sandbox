#!/usr/bin/env bash
# (5) Check the local machine for the tools/permissions needed to build the
#     cluster. Read-only: enables no APIs and creates nothing unless you pass
#     ENABLE_APIS=1 (then it will `gcloud services enable ...`).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

fail=0
note_missing() { warn "MISSING: $1 — $2"; fail=1; }

log "Checking local CLI tooling..."

have gcloud   && ok "gcloud           $(gcloud version 2>/dev/null | head -1)" \
              || note_missing gcloud "install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"

have kubectl  && ok "kubectl          $(kubectl version --client -o yaml 2>/dev/null | grep -m1 gitVersion | awk '{print $2}')" \
              || note_missing kubectl "gcloud components install kubectl  (or your package manager)"

have helm     && ok "helm             $(helm version --short 2>/dev/null)" \
              || note_missing helm "https://helm.sh/docs/intro/install/  (needed to install the KubeRay operator)"

have jq       && ok "jq               $(jq --version 2>/dev/null)" \
              || note_missing jq "needed to resolve the latest agent-sandbox release tag"

have curl     && ok "curl             present" \
              || note_missing curl "needed to fetch agent-sandbox manifests"

# The GKE auth plugin is mandatory for kubectl >= 1.26 to talk to GKE.
if have gke-gcloud-auth-plugin; then
  ok "gke-gcloud-auth-plugin present"
else
  note_missing gke-gcloud-auth-plugin "gcloud components install gke-gcloud-auth-plugin  (required for kubectl to authenticate to GKE)"
fi
# Older gcloud needs this env var exported so kubectl uses the plugin.
if [ "${USE_GKE_GCLOUD_AUTH_PLUGIN:-}" != "True" ]; then
  warn "Consider: export USE_GKE_GCLOUD_AUTH_PLUGIN=True  (forces kubectl to use the auth plugin)"
fi

log "Checking gcloud auth / project context..."
if have gcloud; then
  acct="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
  [ -n "$acct" ] && ok "active account:  $acct" || note_missing "active gcloud login" "run 'gcloud auth login'"
  [ -n "${PROJECT_ID:-}" ] && ok "project:         $PROJECT_ID" || note_missing "project" "set PROJECT_ID in infra/.env"

  # Is the GKE API enabled?
  if gcloud services list --enabled --project "$PROJECT_ID" --format='value(config.name)' 2>/dev/null | grep -q '^container.googleapis.com$'; then
    ok "container.googleapis.com enabled"
  else
    warn "container.googleapis.com NOT enabled."
    if [ "${ENABLE_APIS:-0}" = "1" ]; then
      log "Enabling container.googleapis.com ..."
      gcloud services enable container.googleapis.com --project "$PROJECT_ID"
    else
      warn "  -> re-run with ENABLE_APIS=1 to enable it, or: gcloud services enable container.googleapis.com"
    fi
  fi

  # (best-effort) GPU quota visibility — purely informational.
  log "GPU quota in $REGION (informational; 0 limit => GPU pool creation will fail):"
  gcloud compute regions describe "$REGION" --project "$PROJECT_ID" \
    --flatten=quotas --format="table(quotas.metric, quotas.limit, quotas.usage)" 2>/dev/null \
    | grep -iE 'GPU' || warn "  could not read GPU quotas (continuing)"
fi

echo
if [ "$fail" -ne 0 ]; then
  die "prerequisites incomplete — install the MISSING items above and re-run."
fi
ok "All prerequisites satisfied."
