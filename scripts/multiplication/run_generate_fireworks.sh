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
# instead of Fireworks, run the entrypoint directly with
# `generator.multiply_litellm_model_name=openai/<served-id>` + `OPENAI_BASE_URL=<vLLM>/v1` + `OPENAI_API_KEY`.)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

FIREWORKS_AI_API_KEY="${FIREWORKS_AI_API_KEY:-${FIREWORKS_API_KEY:-}}"
: "${FIREWORKS_AI_API_KEY:?set FIREWORKS_AI_API_KEY to your Fireworks key}"
export FIREWORKS_AI_API_KEY
# See scripts/mini_swe_agent/run_generate_fireworks.sh for the rationale behind these two env vars: the
# generator reaches the LLM via litellm (no SkyRL engine), so we use SkyRL's legacy inference path (the
# new one `import vllm`s even for a remote/no-engine client) and disable Ray's `uv run` hook (it rebuilds
# a broken per-worker venv). Both let `skyrl[skyrl-train]` suffice (no `fsdp`/vllm).
export _SKYRL_USE_NEW_INFERENCE=0
export RAY_ENABLE_UV_RUN_RUNTIME_ENV=0

TOKENIZER="${TOKENIZER:-Qwen/Qwen3-4B}"   # HF id -> tokenizer (model.path)
# Fireworks model id -- VERIFY it exists in your catalog (https://fireworks.ai/models); override FW_MODEL=...
FW_MODEL="${FW_MODEL:-accounts/fireworks/models/gpt-oss-20b}"
DATA_DIR="${DATA_DIR:-$HOME/data/multiply_sandbox}"

# run_engines_locally=false + num_engines=0 -> an empty remote inference client (litellm does generation,
# so no SkyRL engine is needed). logprobs=null: remote mode rejects sampling_params.logprobs.
uv run --extra "${SKYRL_EXTRA:-fsdp}" python -m skyrl_sandbox.multiplication.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  trainer.policy.model.path="$TOKENIZER" \
  generator.multiply_litellm_model_name="fireworks_ai/$FW_MODEL" \
  generator.inference_engine.run_engines_locally=false \
  generator.inference_engine.num_engines=0 \
  trainer.placement.colocate_all=false \
  trainer.eval_batch_size="${EVAL_BATCH_SIZE:-2}" \
  generator.n_samples_per_prompt=1 \
  generator.sampling_params.max_generate_length="${MAX_GEN:-1024}" \
  generator.sampling_params.logprobs=null \
  generator.max_input_length="${MAX_INPUT:-4096}" \
  trainer.max_prompt_length="${MAX_PROMPT:-512}" \
  generator.max_turns="${MAX_TURNS:-3}" \
  trainer.logger="${LOGGER:-console}" \
  trainer.project_name=multiply_sandbox \
  trainer.run_name=multiply_fireworks_gen \
  "$@"
