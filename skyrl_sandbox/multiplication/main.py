"""Training entrypoint for the multiplication-on-agent-sandbox example.

Mirrors ``skyrl_sandbox.mini_swe_agent.main``: a ``BasePPOExp`` subclass whose ``get_generator`` builds
the custom litellm-based :class:`~skyrl_sandbox.multiplication.generator.MultiplyGenerator`. No skyrl-gym
``register`` is needed — the generator drives ``MultiplySandboxEnv`` directly (same pattern as mini-swe).
Training serves the policy on your own vLLM (OpenAI-compatible HTTP) reached via litellm ``openai/`` +
``OPENAI_BASE_URL``; see scripts/multiplication/run_multiply_sandbox.sh.

    uv run --extra fsdp -m skyrl_sandbox.multiplication.main  ...
"""

import sys

import ray
from skyrl.train.config import SkyRLGymConfig, make_config
from skyrl.train.entrypoints.main_base import BasePPOExp, validate_cfg
from skyrl.train.utils import initialize_ray

from .generator import MultiplyGenerator, MultiplyGeneratorConfig

MultiplyConfig = make_config(generator_cls=MultiplyGeneratorConfig)


class MultiplyPPOExp(BasePPOExp):
    def get_generator(self, cfg, tokenizer, inference_engine_client):
        return MultiplyGenerator(
            generator_cfg=cfg.generator,
            skyrl_gym_cfg=SkyRLGymConfig(max_env_workers=0),
            inference_engine_client=inference_engine_client,
            tokenizer=tokenizer,
            model_name=self.cfg.trainer.policy.model.path,
        )


@ray.remote(num_cpus=1)
def skyrl_entrypoint(cfg):
    # make sure the training loop is not run on the head node.
    exp = MultiplyPPOExp(cfg)
    exp.run()


def main() -> None:
    cfg = MultiplyConfig.from_cli_overrides(sys.argv[1:])
    validate_cfg(cfg)
    initialize_ray(cfg)
    ray.get(skyrl_entrypoint.remote(cfg))


if __name__ == "__main__":
    main()
