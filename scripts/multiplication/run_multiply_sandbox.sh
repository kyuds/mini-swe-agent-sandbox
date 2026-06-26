#!/usr/bin/env bash
# Full GRPO training for the multiplication-on-agent-sandbox example (Qwen2.5-1.5B, colocated, GPUs).
# Port of SkyRL's examples/train/multiply/run_multiply.sh, swapping the entrypoint + env for this
# repo's sandbox-backed env. Each trajectory spawns a Sandbox from "multiplication-template" and
# verifies the product via the agent-sandbox SDK's commands.run. See docs/expansion-plan.md §2.
#
# Prereqs:
#   - cluster up (infra/up.sh) with a GPU pool + the gVisor sandbox pool + extensions CRDs.
#   - kubectl apply -f infra/manifests/sandbox-template-multiplication.yaml  (set its image first! caveat)
#   - uv run python -m skyrl_sandbox.multiplication.dataset --output_dir "$DATA_DIR"
set -x
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

DATA_DIR="${DATA_DIR:-$HOME/data/multiply_sandbox}"
NUM_GPUS="${NUM_GPUS:-4}"

uv run --extra fsdp python -m skyrl_sandbox.multiplication.main \
  data.train_data="['$DATA_DIR/train.parquet']" \
  data.val_data="['$DATA_DIR/validation.parquet']" \
  trainer.algorithm.advantage_estimator="grpo" \
  trainer.policy.model.path="${MODEL:-Qwen/Qwen2.5-1.5B-Instruct}" \
  trainer.placement.colocate_all=true \
  trainer.strategy=fsdp \
  trainer.placement.policy_num_gpus_per_node=$NUM_GPUS \
  trainer.placement.ref_num_gpus_per_node=$NUM_GPUS \
  generator.inference_engine.num_engines=$NUM_GPUS \
  generator.inference_engine.tensor_parallel_size=1 \
  trainer.epochs=20 \
  trainer.train_batch_size=1024 \
  trainer.policy_mini_batch_size=256 \
  trainer.micro_forward_batch_size_per_gpu=64 \
  trainer.micro_train_batch_size_per_gpu=64 \
  trainer.eval_batch_size=1024 \
  trainer.eval_before_train=true \
  trainer.eval_interval=5 \
  trainer.max_prompt_length=512 \
  generator.sampling_params.max_generate_length=1024 \
  trainer.policy.optimizer_config.lr=1.0e-6 \
  trainer.algorithm.use_kl_loss=true \
  generator.inference_engine.backend=vllm \
  generator.inference_engine.run_engines_locally=true \
  generator.inference_engine.weight_sync_backend=nccl \
  generator.inference_engine.async_engine=true \
  generator.batched=false \
  environment.env_class=multiply_sandbox \
  generator.n_samples_per_prompt=5 \
  generator.inference_engine.gpu_memory_utilization=0.8 \
  trainer.logger="${LOGGER:-wandb}" \
  trainer.project_name="multiply_sandbox" \
  trainer.run_name="multiply_sandbox_train" \
  "$@"
