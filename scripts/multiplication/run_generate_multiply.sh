#!/usr/bin/env bash
# Generate-only run of the multiplication example on agent-sandbox (NO training, NO GPUs).
# Demonstrates the agent-sandbox SDK path: each trajectory spawns a Sandbox from the
# "multiplication-template" SandboxTemplate and verifies the product with commands.run.
# The LLM is reached via litellm -> OPENAI_BASE_URL (remote endpoint); Ray runs in-process. See
# docs/expansion-plan.md §2.
#
# Prereqs:
#   - infra/up-smoke.sh up (CPU cluster + agent-sandbox + RBAC); the extensions CRDs are installed by
#     default, so SandboxTemplate/Claim work.
#   - kubectl apply -f infra/manifests/sandbox-template-multiplication.yaml  (after setting its image! see caveat)
#   - uv run python -m skyrl_sandbox.multiplication.dataset --output_dir "$DATA_DIR"
#   - OPENAI_API_KEY exported.
#   - Run this INSIDE the cluster (a runner pod) so commands.run reaches the sandbox pod IP. For a laptop
#     run you must make the env use a kubectl tunnel (sandbox in_cluster=false) -- see caveat in
#     skyrl_sandbox/multiplication/sandbox.py.
#
# Same MODEL/tokenizer caveat as the mini-swe generate script (MODEL must be a valid HF id).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

: "${OPENAI_API_KEY:?set OPENAI_API_KEY to your token}"
MODEL="${MODEL:-Qwen/Qwen3-4B}"
DATA_DIR="${DATA_DIR:-$HOME/data/multiply_sandbox}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"   # set to your endpoint serving $MODEL

uv run --extra fsdp python -m skyrl_sandbox.multiplication.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  environment.env_class=multiply_sandbox \
  trainer.policy.model.path="$MODEL" \
  generator.inference_engine.run_engines_locally=false \
  trainer.placement.colocate_all=false \
  generator.batched=false \
  trainer.eval_batch_size="${EVAL_BATCH_SIZE:-2}" \
  generator.n_samples_per_prompt=1 \
  generator.sampling_params.max_generate_length="${MAX_GEN:-1024}" \
  trainer.max_prompt_length="${MAX_PROMPT:-512}" \
  generator.max_turns="${MAX_TURNS:-3}" \
  trainer.logger="${LOGGER:-console}" \
  trainer.project_name=multiply_sandbox \
  trainer.run_name=multiply_sandbox_gen \
  "$@"
