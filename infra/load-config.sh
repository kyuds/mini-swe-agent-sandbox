#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Configuration loader. Reads values from `.env` (local, gitignored) then
# `.env.example` (committed defaults). Nothing is auto-detected or derived —
# PROJECT_ID, REGION and ZONE are all set explicitly in those files.
#
# Edit `.env` / `.env.example` for configuration — NOT this file.
#
# Precedence (highest first):  real environment  >  .env  >  .env.example
# Sourced by every script. Does nothing on its own.
# ---------------------------------------------------------------------------

_CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load a KEY=value file, setting each var ONLY if it is not already set
# (so the real environment and earlier-loaded files take precedence).
_load_env_file() {
  local f="$1" line key val
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"                          # strip comments
    line="${line#"${line%%[![:space:]]*}"}"     # left-trim
    [ -z "$line" ] && continue
    line="${line#export }"                       # tolerate "export KEY=val"
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"                   # trim spaces around key
    [ -z "$key" ] && continue
    val="${val#"${val%%[![:space:]]*}"}"         # left-trim value
    val="${val%"${val##*[![:space:]]}"}"         # right-trim value
    val="${val#\"}"; val="${val%\"}"             # strip surrounding quotes
    val="${val#\'}"; val="${val%\'}"
    [ -z "${!key+x}" ] && export "$key=$val"     # set only if unset
  done < "$f"
  return 0   # don't let the loop's last status (a false `&&` test) propagate & trip `set -e` in callers
}

# .env (local overrides) wins over .env.example (committed defaults).
# Every value — PROJECT_ID, REGION, ZONE included — comes from these files.
_load_env_file "${ENV_FILE:-${_CONFIG_DIR}/.env}"
_load_env_file "${_CONFIG_DIR}/.env.example"
