"""Generate-only entrypoint for the multiplication example (rollouts, NO training).

Reuses SkyRL's ``EvalOnlyEntrypoint`` (``skyrl/train/entrypoints/main_generate.py``) — which builds only
the inference client + the **default** generator and calls ``evaluate`` — and just registers this
repo's ``multiply_sandbox`` env in the Ray task. Use it to validate the agent-sandbox SDK path
(``create_sandbox`` + ``commands.run``) end to end against a remote endpoint, without training/GPUs.

    uv run --extra fsdp -m skyrl_sandbox.multiplication.generate  environment.env_class=multiply_sandbox ...

See ``scripts/multiplication/run_generate_multiply.sh`` and ``docs/expansion-plan.md`` §2.
"""

import asyncio
import sys

import ray
from skyrl.train.config import SkyRLTrainConfig
from skyrl.train.entrypoints.main_generate import EvalOnlyEntrypoint
from skyrl.train.utils import initialize_ray
from skyrl.train.utils.utils import validate_generator_cfg
from skyrl_gym.envs import register


@ray.remote(num_cpus=1)
def generate_entrypoint(cfg: SkyRLTrainConfig) -> dict:
    register(id="multiply_sandbox", entry_point="skyrl_sandbox.multiplication.env:MultiplySandboxEnv")
    exp = EvalOnlyEntrypoint(cfg)
    inference_engine_client = exp.get_inference_client()
    return asyncio.run(exp.run(inference_engine_client))


def main() -> None:
    cfg = SkyRLTrainConfig.from_cli_overrides(sys.argv[1:])
    validate_generator_cfg(cfg)
    initialize_ray(cfg)
    metrics = ray.get(generate_entrypoint.remote(cfg))
    print(f"[multiplication generate-only] metrics: {metrics}")


if __name__ == "__main__":
    main()
