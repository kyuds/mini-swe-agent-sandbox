"""mini-swe-agent ↔ kubernetes-sigs/agent-sandbox integration.

Exposes ``AgentSandboxEnvironment``, a drop-in mini-swe-agent ``Environment`` that runs each
SWE-bench box as an agent-sandbox ``Sandbox`` pod. Select it via mini-swe-agent's dotted-path env
factory: ``environment_class: "skyrl_sandbox.mini_swe_agent.environment.AgentSandboxEnvironment"``.
"""

from .environment import AgentSandboxEnvironment, AgentSandboxEnvironmentConfig

__all__ = ["AgentSandboxEnvironment", "AgentSandboxEnvironmentConfig"]
