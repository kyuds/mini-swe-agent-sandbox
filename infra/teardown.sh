#!/usr/bin/env bash
# Delete the entire cluster (removes all node pools, operators, RBAC, sandboxes).
#   ASSUME_YES=1 ./teardown.sh
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

warn "This DELETES the cluster '$CLUSTER_NAME' in $ZONE and everything in it."
confirm "Really delete?" || die "aborted."

log "Deleting cluster (this takes a few minutes)..."
gcloud container clusters delete "$CLUSTER_NAME" $(gcloud_location) --project "$PROJECT_ID" --quiet
ok "cluster deleted."
