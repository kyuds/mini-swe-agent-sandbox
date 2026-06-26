"""Generate-only entrypoint for the multiplication example (rollouts, NO training).

Mirrors ``skyrl_sandbox.mini_swe_agent.generate``: builds only the inference client + the custom
:class:`~skyrl_sandbox.multiplication.generator.MultiplyGenerator` and calls ``evaluate`` — no trainer.
Because the generator calls litellm directly, this can hit Fireworks (``fireworks_ai/…``) for a no-GPU
demo; see scripts/multiplication/run_generate_fireworks.sh. Shares mini-swe's generate-only caveats.

    uv run --extra fsdp -m skyrl_sandbox.multiplication.generate  ...
"""

import asyncio
import sys

import ray
from skyrl.train.config import SkyRLGymConfig, make_config
from skyrl.train.entrypoints.main_base import BasePPOExp
from skyrl.train.evaluate import evaluate, evaluate_step_wise
from skyrl.train.utils import initialize_ray
from skyrl.train.utils.trainer_utils import build_dataloader
from skyrl.train.utils.utils import validate_generator_cfg
from skyrl.backends.skyrl_train.inference_engines.base import InferenceEngineInterface

from .generator import MultiplyGenerator, MultiplyGeneratorConfig

MultiplyConfig = make_config(generator_cls=MultiplyGeneratorConfig)


class MultiplyGenerateExp(BasePPOExp):
    """Eval/generate-only: build just the generator + inference client, run ``evaluate``, no trainer."""

    def get_train_dataset(self):
        return None

    def get_generator(self, cfg, tokenizer, inference_engine_client):
        return MultiplyGenerator(
            generator_cfg=cfg.generator,
            skyrl_gym_cfg=SkyRLGymConfig(max_env_workers=0),
            inference_engine_client=inference_engine_client,
            tokenizer=tokenizer,
            model_name=self.cfg.trainer.policy.model.path,
        )

    async def run(self, inference_engine_client: InferenceEngineInterface) -> dict:
        assert self.eval_dataset is not None, "generate-only requires an eval dataset (set data.val_data)"
        # The multiply generator drives the LLM via litellm directly (Fireworks / remote OpenAI), not the
        # SkyRL inference engine, so this path runs with no engines (num_engines=0). Only wake real engines
        # (wake_up() HTTP-POSTs each remote engine and asserts >0 engines, so it must be skipped here).
        if getattr(inference_engine_client, "engines", None):
            await inference_engine_client.wake_up()
        generator = self.get_generator(self.cfg, self.tokenizer, inference_engine_client)
        eval_fn = evaluate_step_wise if self.cfg.generator.step_wise_trajectories else evaluate
        results = await eval_fn(
            eval_dataloader=build_dataloader(self.cfg, self.eval_dataset, is_train=False),
            generator=generator,
            cfg=self.cfg,
            global_step=None,
            tokenizer=self.tokenizer,
        )
        self.get_tracker().log(results, step=0, commit=True)
        return results


@ray.remote(num_cpus=1)
def generate_entrypoint(cfg) -> dict:
    exp = MultiplyGenerateExp(cfg)
    inference_engine_client = exp.get_inference_client()
    return asyncio.run(exp.run(inference_engine_client))


def main() -> None:
    cfg = MultiplyConfig.from_cli_overrides(sys.argv[1:])
    validate_generator_cfg(cfg)
    initialize_ray(cfg)
    metrics = ray.get(generate_entrypoint.remote(cfg))
    print(f"[multiplication generate-only] metrics: {metrics}")


if __name__ == "__main__":
    main()
