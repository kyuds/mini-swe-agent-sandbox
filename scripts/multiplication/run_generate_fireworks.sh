#!/usr/bin/env bash
# Generate-only run of the multiplication example against FIREWORKS (NO training, NO GPUs), via litellm's
# native fireworks_ai provider. Mirrors scripts/mini_swe_agent/run_generate_fireworks.sh. Each trajectory
# adopts a Sandbox from "multiplication-pool" and verifies the product with the SDK's commands.run.
#
# Prereqs:
#   - infra/up-smoke.sh up + extensions CRDs; apply sandbox-template + sandbox-warmpool manifests (set the image!).
#   - uv run python -m skyrl_sandbox.multiplication.dataset --output_dir "$DATA_DIR"
#   - FIREWORKS_AI_API_KEY exported.
#   - Run INSIDE the cluster (runner pod) so commands.run reaches the sandbox pod IP (else set the env's
#     in_cluster=false for a kubectl tunnel -- see skyrl_sandbox/multiplication/sandbox.py).
#
# TOKENIZER (model.path, a valid HF id) is decoupled from FW_MODEL (the Fireworks model). The tokenizer
# is a stand-in for the served model -- fine for generation, not training. (To target your own vLLM
# instead of Fireworks: set MODEL=openai/<id> + OPENAI_BASE_URL=<vLLM>/v1 + OPENAI_API_KEY, drop FW_MODEL.)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

FIREWORKS_AI_API_KEY="${FIREWORKS_AI_API_KEY:-${FIREWORKS_API_KEY:-}}"
: "${FIREWORKS_AI_API_KEY:?set FIREWORKS_AI_API_KEY to your Fireworks key}"
export FIREWORKS_AI_API_KEY

TOKENIZER="${TOKENIZER:-Qwen/Qwen3-4B}"   # HF id -> tokenizer (model.path)
# Fireworks model id -- VERIFY it exists in your catalog (https://fireworks.ai/models); override FW_MODEL=...
FW_MODEL="${FW_MODEL:-accounts/fireworks/models/qwen3-4b}"
DATA_DIR="${DATA_DIR:-$HOME/data/multiply_sandbox}"

uv run --extra fsdp python -m skyrl_sandbox.multiplication.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  trainer.policy.model.path="$TOKENIZER" \
  generator.multiply_litellm_model_name="fireworks_ai/$FW_MODEL" \
  generator.inference_engine.run_engines_locally=false \
  trainer.placement.colocate_all=false \
  trainer.eval_batch_size="${EVAL_BATCH_SIZE:-2}" \
  generator.n_samples_per_prompt=1 \
  generator.sampling_params.max_generate_length="${MAX_GEN:-1024}" \
  generator.max_input_length="${MAX_INPUT:-4096}" \
  trainer.max_prompt_length="${MAX_PROMPT:-512}" \
  generator.max_turns="${MAX_TURNS:-3}" \
  trainer.logger="${LOGGER:-console}" \
  trainer.project_name=multiply_sandbox \
  trainer.run_name=multiply_fireworks_gen \
  "$@"
