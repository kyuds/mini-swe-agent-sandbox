#!/usr/bin/env bash
# Generate-only run of the mini-swe-agent example against FIREWORKS (NO training, NO GPUs), via
# litellm's NATIVE fireworks_ai provider -- no OPENAI_* hijack. The agent's bash runs in agent-sandbox
# pods via AgentSandboxEnvironment; Ray runs in-process (local mode). See docs/expansion-plan.md §1.
#
# Prereqs:
#   - agent-sandbox cluster up (infra/up-smoke.sh is enough -- no GPU pool) + kubectl context set, OR run
#     this inside the runner pod (scripts/mini_swe_agent/run_smoke_in_pod.sh style) for real RBAC.
#   - eval dataset:  uv run python -m skyrl_sandbox.mini_swe_agent.preprocess --output_dir "$DATA_DIR"
#   - FIREWORKS_AI_API_KEY exported (litellm's native env var for the fireworks_ai provider).
#
# TOKENIZER vs MODEL are DECOUPLED (via generator.miniswe_litellm_model_name -- see generator.py):
#   * TOKENIZER (= trainer.policy.model.path) must be a valid HF id; it only loads the tokenizer. Default Qwen/Qwen3-4B.
#   * FW_MODEL  (= the Fireworks model id) becomes the full litellm id `fireworks_ai/$FW_MODEL`.
#   The tokenizer is a STAND-IN for the served model -- fine for a generation test (token-id bookkeeping
#   only; the agent's text comes from Fireworks, the reward from running the eval in the sandbox), NOT
#   for training.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

# litellm's fireworks_ai provider reads FIREWORKS_AI_API_KEY (accept FIREWORKS_API_KEY as an alias).
FIREWORKS_AI_API_KEY="${FIREWORKS_AI_API_KEY:-${FIREWORKS_API_KEY:-}}"
: "${FIREWORKS_AI_API_KEY:?set FIREWORKS_AI_API_KEY to your Fireworks key}"
export FIREWORKS_AI_API_KEY
# Optional litellm cost/context registry (not required for fireworks_ai; litellm has built-in data).
export LITELLM_MODEL_REGISTRY_PATH="${LITELLM_MODEL_REGISTRY_PATH:-configs/mini_swe_agent/litellm.json}"

TOKENIZER="${TOKENIZER:-Qwen/Qwen3-4B}"   # HF id -> tokenizer (model.path)
# Fireworks model id -- VERIFY it exists in your catalog (https://fireworks.ai/models); override with
# FW_MODEL=... if your slug differs (the tokenizer above stays the same regardless).
FW_MODEL="${FW_MODEL:-accounts/fireworks/models/qwen3-4b}"
DATA_DIR="${DATA_DIR:-$HOME/data/swe_gym_subset}"
CONFIG="${CONFIG:-$REPO_DIR/configs/mini_swe_agent/swebench_agent_sandbox.yaml}"

# run_engines_locally=false -> RemoteInferenceEngine (no local vLLM/GPU); colocate_all=false -> no GPU
# placement group. eval-only knobs keep the batch tiny for a quick validation.
uv run --extra fsdp python -m skyrl_sandbox.mini_swe_agent.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  trainer.policy.model.path="$TOKENIZER" \
  generator.miniswe_litellm_model_name="fireworks_ai/$FW_MODEL" \
  generator.inference_engine.run_engines_locally=false \
  trainer.placement.colocate_all=false \
  trainer.eval_batch_size="${EVAL_BATCH_SIZE:-2}" \
  generator.n_samples_per_prompt=1 \
  generator.sampling_params.max_generate_length="${MAX_GEN:-2048}" \
  generator.max_input_length="${MAX_INPUT:-8192}" \
  generator.max_turns="${MAX_TURNS:-10}" \
  generator.miniswe_config_path="$CONFIG" \
  generator.miniswe_traj_dir="${MINISWE_TRAJ_DIR:-$HOME/mini_swe_agent_trajs_gen}" \
  trainer.logger="${LOGGER:-console}" \
  trainer.project_name=mini_swe_gen \
  trainer.run_name=mini_swe_fireworks_gen \
  "$@"
