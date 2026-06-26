"""Training entrypoint for the multiplication-on-agent-sandbox example.

Mirrors SkyRL's ``examples/train/multiply/main_multiply.py``: register the env inside the Ray task
(no skyrl-gym source change), then run the standard ``BasePPOExp`` with the **default** generator (the
task is a plain multi-turn ``BaseTextEnv``, so no custom generator is needed — unlike mini-swe-agent).

    uv run --extra fsdp -m skyrl_sandbox.multiplication.main  environment.env_class=multiply_sandbox ...

See ``scripts/multiplication/run_multiply_sandbox.sh`` and ``docs/expansion-plan.md`` §2.
"""

import sys

import ray
from skyrl.train.config import SkyRLTrainConfig
from skyrl.train.entrypoints.main_base import BasePPOExp, validate_cfg
from skyrl.train.utils import initialize_ray
from skyrl_gym.envs import register


@ray.remote(num_cpus=1)
def skyrl_entrypoint(cfg: SkyRLTrainConfig):
    register(id="multiply_sandbox", entry_point="skyrl_sandbox.multiplication.env:MultiplySandboxEnv")
    # make sure the training loop is not run on the head node.
    exp = BasePPOExp(cfg)
    exp.run()


def main() -> None:
    cfg = SkyRLTrainConfig.from_cli_overrides(sys.argv[1:])
    validate_cfg(cfg)
    initialize_ray(cfg)
    ray.get(skyrl_entrypoint.remote(cfg))


if __name__ == "__main__":
    main()
