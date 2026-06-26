#!/usr/bin/env bash
# Generate-only run of the mini-swe-agent example against an OpenAI-compatible endpoint (NO training,
# NO GPUs). The LLM is reached via litellm -> OPENAI_BASE_URL; the agent's bash runs in agent-sandbox
# pods via AgentSandboxEnvironment; Ray runs in-process (local mode). See docs/expansion-plan.md §1.
#
# Prereqs:
#   - agent-sandbox cluster up (infra/up-smoke.sh is enough -- no GPU pool needed) and kubectl context set,
#     OR run this inside the runner pod (scripts/mini_swe_agent/run_smoke_in_pod.sh style) for real RBAC.
#   - a preprocessed eval dataset:  uv run python -m skyrl_sandbox.mini_swe_agent.preprocess --output_dir "$DATA_DIR"
#   - OPENAI_API_KEY exported (your token).
#
# IMPORTANT caveat (docs/expansion-plan.md §5.2-5.3):
#   MODEL is used BOTH as the litellm model name (openai/$MODEL, must have an entry in litellm.json)
#   AND to load a Hugging Face tokenizer. So MODEL must be a valid HF model id. Default is a small Qwen
#   served by your endpoint. Real OpenAI gpt-* ids (e.g. gpt-4o-mini) will FAIL the tokenizer load
#   unless you decouple the tokenizer path -- point OPENAI_BASE_URL at an endpoint serving an open HF
#   model instead (vLLM/Together/Fireworks/etc.).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

: "${OPENAI_API_KEY:?set OPENAI_API_KEY to your token (litellm reads it for the openai/ provider)}"
MODEL="${MODEL:-Qwen/Qwen3-4B}"                                   # HF id; needs an openai/<MODEL> entry in litellm.json
DATA_DIR="${DATA_DIR:-$HOME/data/swe_gym_subset}"
CONFIG="${CONFIG:-$REPO_DIR/configs/mini_swe_agent/swebench_agent_sandbox.yaml}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"   # set to your endpoint serving $MODEL
export LITELLM_MODEL_REGISTRY_PATH="${LITELLM_MODEL_REGISTRY_PATH:-configs/mini_swe_agent/litellm.json}"

# run_engines_locally=false -> RemoteInferenceEngine (no local vLLM/GPU); colocate_all=false -> no GPU
# placement group. eval-only knobs keep the batch tiny for a quick validation.
uv run --extra fsdp python -m skyrl_sandbox.mini_swe_agent.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  trainer.policy.model.path="$MODEL" \
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
  trainer.run_name=mini_swe_openai_gen \
  "$@"
