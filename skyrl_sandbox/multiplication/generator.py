"""Custom litellm-based generator for the multiplication example.

Mirrors `skyrl_sandbox.mini_swe_agent.generator.MiniSweAgentGenerator` so multiplication gets the SAME
dual-LLM-backend suite as mini-swe: the LLM is called via **litellm directly** (not SkyRL's
RemoteInferenceEngine), so it can use any provider — Fireworks (`fireworks_ai/…`, generation demo) or
your own vLLM (`openai/<model.path>` + `OPENAI_BASE_URL`, training) — selected by the decoupled
`multiply_litellm_model_name`. (The default SkyRL gym generator can't do this: its RemoteInferenceEngine
sends no auth header, so it can't reach a keyed provider like Fireworks.)

Each trajectory drives :class:`~skyrl_sandbox.multiplication.env.MultiplySandboxEnv` directly (it creates
the agent-sandbox Sandbox, verifies the product via the SDK's commands.run, and is deleted on close).

Status: untested on a cluster; shares mini-swe's generate-only caveats (docs/expansion-plan.md §5).
"""

import asyncio
import traceback
from dataclasses import dataclass
from typing import Any, Dict

import ray
from minisweagent.models import get_model

from .env import MultiplySandboxEnv

from skyrl.train.config import GeneratorConfig, SkyRLGymConfig
from skyrl.train.generators.skyrl_gym_generator import SkyRLGymGenerator, GeneratorOutput, GeneratorInput
from skyrl.train.generators.base import TrajectoryID, BatchMetadata
from skyrl.backends.skyrl_train.inference_engines.base import ConversationType
from skyrl.backends.skyrl_train.inference_engines.inference_engine_client import InferenceEngineClient
from skyrl.backends.skyrl_train.inference_engines.utils import get_sampling_params_for_backend
from skyrl.train.generators.utils import (
    get_rollout_metrics,
    get_response_ids_and_loss_mask_from_messages,
)


@dataclass
class MultiplyGeneratorConfig(GeneratorConfig):
    """Extended generator config for the multiplication example."""

    multiply_litellm_model_name: str = ""
    """Full provider-qualified litellm model id, used verbatim (e.g.
    ``fireworks_ai/accounts/fireworks/models/qwen3-4b`` for the Fireworks generation demo, or
    ``openai/gpt-4o-mini``). Empty -> ``openai/<model.path>`` (SkyRL local-vLLM / training). Decoupled
    from ``trainer.policy.model.path`` (which loads the tokenizer). Same field as mini-swe's
    ``miniswe_litellm_model_name``."""


@ray.remote(num_cpus=0.01)
def init_and_run(prompt: list, env_extras: dict, litellm_model_name: str, sampling_params: dict, max_turns: int):
    """One trajectory: drive MultiplySandboxEnv (sandbox via commands.run) with an LLM reached by litellm."""
    import logging

    logger = logging.getLogger(__name__)
    # mini-swe-agent's litellm wrapper: get_model(name, {"model_kwargs": {...}}).query(messages)->{"content"}.
    model = get_model(litellm_model_name, {"model_kwargs": {"drop_params": True, **sampling_params}})

    env = None
    error = None
    reward = 0.0
    messages = list(prompt)  # [system, user("a * b")]
    try:
        env = MultiplySandboxEnv(env_config={}, extras=env_extras)
        env.init(messages)  # creates the Sandbox + computes the product in-pod (commands.run)
        for _ in range(max_turns):
            content = model.query(messages)["content"]
            messages.append({"role": "assistant", "content": content})
            step_out = env.step(content)  # verifies the boxed answer against the sandbox-computed product
            reward = float(step_out["reward"])
            if step_out["done"]:
                break
            messages.extend(step_out["observations"])  # feedback -> retry
    except Exception as e:
        logger.error(f"multiply rollout error: {e}", exc_info=True)
        error = str(e)
    finally:
        if env is not None:
            env.close()  # delete the Sandbox
    return messages, reward, error


class MultiplyGenerator(SkyRLGymGenerator):
    """Multiplication generator that calls the LLM via litellm (so any provider works). Mirrors mini-swe."""

    def __init__(
        self,
        generator_cfg: GeneratorConfig,
        skyrl_gym_cfg: SkyRLGymConfig,
        inference_engine_client: InferenceEngineClient,
        tokenizer,
        model_name: str,
    ):
        super().__init__(generator_cfg, skyrl_gym_cfg, inference_engine_client, tokenizer)
        self.generator_cfg = generator_cfg
        self.tokenizer = tokenizer
        self.model_name = model_name
        # Decoupled full litellm id (verbatim); empty -> openai/<model.path> (local vLLM). See config field.
        self.litellm_model_name = generator_cfg.multiply_litellm_model_name or ("openai/" + self.model_name)
        if self.generator_cfg.chat_template.name_or_path is not None:
            raise NotImplementedError("MultiplyGenerator doesn't support custom chat template")

    async def multiply_agent_loop(
        self,
        prompt: ConversationType,
        env_extras: Dict[str, Any],
        max_tokens: int,
        max_input_length: int,
        sampling_params: Dict[str, Any],
        trajectory_id: TrajectoryID,
        batch_metadata: BatchMetadata,
    ):
        messages, reward, error = await init_and_run.remote(
            prompt, env_extras, self.litellm_model_name, sampling_params, self.generator_cfg.max_turns
        )
        if not len(messages):
            return None, None, None, None, None, None

        # messages[0]=system, messages[1]=user("a * b"); messages[2:] are assistant/feedback turns.
        for message in messages[:2]:
            assert message["role"] in ("system", "user"), "Expected the first two messages to be system + user"
        response_messages = messages[2:]
        initial_input_ids = self.tokenizer.apply_chat_template(
            messages[:2], add_generation_prompt=False, return_dict=False, tokenize=True
        )
        initial_prompt_length = len(initial_input_ids)

        # End on an assistant message (drop a trailing "user" feedback turn if the last attempt was wrong).
        last_idx = len(response_messages) - 1
        while last_idx >= 0 and response_messages[last_idx]["role"] == "user":
            last_idx -= 1
        if last_idx < 0:
            return None, None, None, None, None, None
        response_messages = response_messages[: last_idx + 1]

        response_ids, loss_mask, _ = get_response_ids_and_loss_mask_from_messages(
            response_messages, self.tokenizer, assistant_logprobs=None
        )
        prompt_ids = initial_input_ids
        max_response_tokens = max_tokens + max_input_length - initial_prompt_length
        stop_reason = "complete"
        if len(response_ids) > max_response_tokens:
            stop_reason = "length"
        response_ids = response_ids[:max_response_tokens]
        loss_mask = loss_mask[:max_response_tokens]
        return (response_ids, reward, stop_reason, loss_mask, prompt_ids, None)

    async def generate(self, input_batch: GeneratorInput) -> GeneratorOutput:
        prompts = input_batch["prompts"]
        env_extras = input_batch["env_extras"]
        trajectory_ids = input_batch["trajectory_ids"]
        batch_metadata = input_batch["batch_metadata"]
        max_tokens = self.generator_cfg.sampling_params.max_generate_length
        max_input_length = self.generator_cfg.max_input_length
        sampling_params = get_sampling_params_for_backend(
            self.generator_cfg.inference_engine.backend, self.generator_cfg.sampling_params
        )

        tasks = [
            self.multiply_agent_loop(
                prompts[i],
                env_extras[i],
                max_tokens=max_tokens,
                max_input_length=max_input_length,
                sampling_params=sampling_params,
                trajectory_id=trajectory_ids[i],
                batch_metadata=batch_metadata,
            )
            for i in range(len(prompts))
        ]
        all_outputs = await asyncio.gather(*tasks)

        responses = [o[0] for o in all_outputs if o[0] is not None]
        rewards = [o[1] for o in all_outputs if o[0] is not None]
        stop_reasons = [o[2] for o in all_outputs if o[0] is not None]
        loss_masks = [o[3] for o in all_outputs if o[0] is not None]
        prompt_token_ids = [o[4] for o in all_outputs if o[0] is not None]
        if not len(responses):
            raise ValueError("No valid responses this step (all multiplication trajectories failed).")
        rollout_metrics = get_rollout_metrics(responses, rewards)

        generator_output: GeneratorOutput = {
            "prompt_token_ids": prompt_token_ids,
            "response_ids": responses,
            "rewards": rewards,
            "loss_masks": loss_masks,
            "stop_reasons": stop_reasons,
            "rollout_metrics": rollout_metrics,
            "rollout_logprobs": None,
        }
        return generator_output
