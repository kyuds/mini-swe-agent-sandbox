"""Toy multiplication task on agent-sandbox via the SDK (SandboxTemplate + ``commands.run``).

The single-image counterpart to :mod:`skyrl_sandbox.mini_swe_agent`: one fixed image spawned from a
``SandboxTemplate`` with ``SandboxClient.create_sandbox``, executed with ``commands.run`` (the in-image
``:8888`` runtime).
"""

from .env import MultiplySandboxEnv
from .sandbox import MultiplicationSandbox, MultiplicationSandboxConfig

__all__ = ["MultiplySandboxEnv", "MultiplicationSandbox", "MultiplicationSandboxConfig"]
