#!/usr/bin/env bash
# Generate-only run of the multiplication example on agent-sandbox (NO training). Each trajectory spawns
# a Sandbox from the "multiplication-pool" warm pool and verifies the product with the SDK's commands.run.
#
# LLM PATH (important — differs from mini-swe): the multiplication example uses SkyRL's DEFAULT generator
# -> RemoteInferenceEngine, which posts to generator.inference_engine.remote_urls and sends NO auth
# header. So it ONLY works against a NO-AUTH, OpenAI-compatible endpoint (your own vLLM) -- it CANNOT use
# Fireworks/OpenAI, which require an API key the engine can't send. (The mini-swe example calls litellm
# directly, which is why IT can use Fireworks; multiplication can't.)
#   * For a hosted/no-GPU demo, use the mini-swe example (run_generate_fireworks.sh).
#   * For multiplication, either point REMOTE_URL at your own vLLM (below), or just run training
#     (scripts/multiplication/run_multiply_sandbox.sh) -- training exercises the SAME commands.run path
#     during rollouts, so it's the simpler agent-sandbox demo for this example.
#
# Prereqs:
#   - infra/up-smoke.sh up + extensions CRDs; apply sandbox-template + sandbox-warmpool manifests (set the image!).
#   - uv run python -m skyrl_sandbox.multiplication.dataset --output_dir "$DATA_DIR"
#   - REMOTE_URL = HOST:PORT of a running OpenAI-compatible vLLM serving $MODEL (NO http:// — the engine
#     prepends it), reachable from the generator. No auth.
#   - Run INSIDE the cluster (runner pod) so commands.run reaches the sandbox pod IP (else set the env's
#     in_cluster=false for a kubectl tunnel -- see skyrl_sandbox/multiplication/sandbox.py).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

MODEL="${MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"                 # HF id the vLLM serves (also the tokenizer)
REMOTE_URL="${REMOTE_URL:?set REMOTE_URL to your OpenAI-compatible vLLM as HOST:PORT (no http://)}"
DATA_DIR="${DATA_DIR:-$HOME/data/multiply_sandbox}"

uv run --extra fsdp python -m skyrl_sandbox.multiplication.generate \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  environment.env_class=multiply_sandbox \
  trainer.policy.model.path="$MODEL" \
  generator.inference_engine.run_engines_locally=false \
  generator.inference_engine.remote_urls="['$REMOTE_URL']" \
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
