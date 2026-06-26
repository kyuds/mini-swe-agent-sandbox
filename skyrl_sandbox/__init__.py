"""skyrl-sandbox: run SkyRL RL workloads on kubernetes-sigs/agent-sandbox.

Two examples, one per subpackage:

* :mod:`skyrl_sandbox.mini_swe_agent` — SWE-bench with mini-swe-agent. Per-instance images, so each
  trajectory creates a raw ``Sandbox`` CR (no template) and execs via the Kubernetes pod-exec API.
* :mod:`skyrl_sandbox.multiplication` — a toy single-image task that spawns from an agent-sandbox
  ``SandboxTemplate`` and executes via the agent-sandbox Python SDK (``create_sandbox`` +
  ``commands.run``).

See ``docs/expansion-plan.md`` for the design and ``docs/agent-sandbox-research.md`` for the
template/warm-pool research that motivates the two different backends.
"""
