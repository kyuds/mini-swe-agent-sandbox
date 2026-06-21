#!/usr/bin/env bash
# Tear down everything up-smoke.sh created: deletes the whole cluster (default pool + gVisor pool +
# agent-sandbox controller + RBAC + any sandboxes). Counterpart to up-smoke.sh.
#
#   ASSUME_YES=1 ./teardown-smoke.sh     # skip the confirmation prompt
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/load-config.sh"
source "${SCRIPT_DIR}/lib.sh"

require_cmd gcloud
require_project

if ! cluster_exists; then
  ok "cluster '$CLUSTER_NAME' does not exist — nothing to delete."
  exit 0
fi

warn "This DELETES the cluster '$CLUSTER_NAME' in $ZONE and everything in it (gVisor pool, controller, RBAC, sandboxes)."
confirm "Really delete?" || die "aborted."

log "Deleting cluster (this takes a few minutes)..."
gcloud container clusters delete "$CLUSTER_NAME" $(gcloud_location) --project "$PROJECT_ID" --quiet
ok "cluster deleted."
