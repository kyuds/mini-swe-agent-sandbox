#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Shared helpers: logging, command checks, confirmation, idempotency guards.
# Sourced by every numbered script. Does not run anything on its own.
# ---------------------------------------------------------------------------

# --- pretty logging ---------------------------------------------------------
_c() { [ -t 1 ] && printf '%s' "$1" || true; }   # color only on a tty
log()  { printf '%s[infra]%s %s\n' "$(_c $'\033[1;34m')" "$(_c $'\033[0m')" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$(_c $'\033[1;32m')" "$(_c $'\033[0m')" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$(_c $'\033[1;33m')" "$(_c $'\033[0m')" "$*" >&2; }
err()  { printf '%s[err ]%s %s\n' "$(_c $'\033[1;31m')" "$(_c $'\033[0m')" "$*" >&2; }
die()  { err "$*"; exit 1; }

# --- command / context checks ----------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

require_cmd() {
  have "$1" || die "required command not found: '$1' (${2:-install it and re-run})"
}

require_project() {
  [ -n "${PROJECT_ID:-}" ] || die "PROJECT_ID is empty — set it in infra/.env (copy from .env.example)."
}

# Standard gcloud location flag. Swap to "--region $REGION" for a regional cluster.
gcloud_location() { printf -- '--zone %s' "$ZONE"; }

# --- confirmation (skipped when ASSUME_YES=1) ------------------------------
confirm() {
  local prompt="${1:-Proceed?}"
  if [ "${ASSUME_YES:-0}" = "1" ]; then return 0; fi
  read -r -p "$prompt [y/N] " ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --- idempotency guards -----------------------------------------------------
cluster_exists() {
  gcloud container clusters describe "$CLUSTER_NAME" $(gcloud_location) \
    --project "$PROJECT_ID" >/dev/null 2>&1
}

nodepool_exists() {
  gcloud container node-pools describe "$1" --cluster "$CLUSTER_NAME" \
    $(gcloud_location) --project "$PROJECT_ID" >/dev/null 2>&1
}

# kubectl bound to our cluster context (assumes get-credentials already ran).
kc() { kubectl "$@"; }
