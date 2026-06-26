"""Multiplication task whose ground-truth product is computed INSIDE an agent-sandbox pod.

Port of SkyRL's ``examples/train/multiply/env.py`` (``MultiplyEnv``). The task is unchanged — the model
must answer ``{a} * {b}`` in ``\\boxed{...}`` — but instead of computing the product in pure Python, we
run ``python3 -c "print(a*b)"`` in an agent-sandbox ``Sandbox`` via the SDK's ``commands.run`` and use
the sandbox's stdout as the source of truth. This exercises the single-image + SandboxTemplate +
``commands.run`` path end to end. See ``docs/expansion-plan.md`` §2.

Driven directly by :class:`~skyrl_sandbox.multiplication.generator.MultiplyGenerator` — its per-trajectory
rollout calls ``init`` (create the sandbox) / ``step`` (verify) / ``close`` (delete). No skyrl-gym
registration (the custom generator builds this env itself, the same pattern as mini-swe). The sandbox
knobs (``MultiplicationSandboxConfig``: warmpool, namespace, in_cluster, …) currently use defaults —
the generator constructs the env with ``env_config={}`` (a laptop/local-Ray run needs ``in_cluster=False``,
which would require threading that through).

NOTE: the task itself doesn't *need* a sandbox (the reward is trivial); running the check in the pod
is the point — it demonstrates the agent-sandbox SDK execution path. A tool-use variant (model issues
shell commands the env runs in the sandbox) is the natural, more realistic extension.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from skyrl_gym.envs.base_text_env import BaseTextEnv, BaseTextEnvStepOutput

from .sandbox import MultiplicationSandbox, MultiplicationSandboxConfig


class MultiplySandboxEnv(BaseTextEnv):
    """Multiplication env whose reward is verified by running the multiplication in an agent-sandbox pod."""

    def __init__(self, env_config: Dict[str, Any] = {}, extras: Dict[str, Any] = {}):
        super().__init__()
        assert "reward_spec" in extras, "reward_spec field is required"
        assert "ground_truth" in extras["reward_spec"], "ground_truth is required in reward_spec field"
        self.ground_truth = str(extras["reward_spec"]["ground_truth"]).strip()

        # Operands (added by this repo's dataset.py) so the sandbox can recompute the product itself.
        extra_info = extras.get("extra_info", {}) or {}
        self.num1 = extra_info.get("num1")
        self.num2 = extra_info.get("num2")

        self.max_turns = extras.get("max_turns", 5)

        # Sandbox knobs come from the YAML `environment:` block (env_config); everything has a default
        # that matches this repo's infra, so it works out of the box.
        self.sandbox_cfg = MultiplicationSandboxConfig(
            **{k: v for k, v in (env_config or {}).items() if k in MultiplicationSandboxConfig.__dataclass_fields__}
        )
        self._sandbox: MultiplicationSandbox | None = None
        self._sandbox_truth: str | None = None

    # --- lifecycle -----------------------------------------------------------------------------

    def init(self, prompt):
        """Create the sandbox once per trajectory and compute the product in it (the demonstration)."""
        self._sandbox = MultiplicationSandbox(self.sandbox_cfg)
        self._sandbox.start()
        self._sandbox_truth = self._compute_truth_in_sandbox()
        return prompt, {}

    def close(self):
        if self._sandbox is not None:
            self._sandbox.delete()
            self._sandbox = None

    # --- task ----------------------------------------------------------------------------------

    def _compute_truth_in_sandbox(self) -> str:
        """Run the multiplication in the pod via commands.run; fall back to the dataset ground truth."""
        if self._sandbox is None or self.num1 is None or self.num2 is None:
            return self.ground_truth
        res = self._sandbox.run(f"python3 -c 'print({int(self.num1)} * {int(self.num2)})'")
        out = (res.get("stdout") or "").strip()
        return out if res.get("returncode") == 0 and out else self.ground_truth

    @staticmethod
    def _parse_action(action: str):
        match = re.search(r"\\boxed\{([^}]+)\}", action)
        return match.group(1) if match else None

    def step(self, action: str) -> BaseTextEnvStepOutput:
        self.turns += 1
        answer = self._parse_action(action)
        truth = self._sandbox_truth if self._sandbox_truth is not None else self.ground_truth

        is_correct = answer is not None and answer.strip() == truth
        found_boxed = answer is not None
        done = self.turns >= self.max_turns or is_correct
        reward = 1.0 if is_correct else (0.5 if found_boxed else 0.0)

        if done:
            return BaseTextEnvStepOutput(
                observations=[],
                reward=reward,
                done=True,
                metadata={"parsed_answer": answer, "sandbox_truth": truth},
            )

        if answer is not None:
            feedback = f"Your answer '{answer}' is incorrect. Please try again."
        else:
            feedback = "Please provide your answer in the format \\boxed{your_answer}."
        return BaseTextEnvStepOutput(
            observations=[{"role": "user", "content": feedback}],
            reward=0.0,
            done=False,
            metadata={"parsed_answer": answer},
        )
