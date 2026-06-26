"""agent-sandbox SDK wrapper for the multiplication example.

This is the *opposite* design point from the mini-swe-agent backend. SWE-bench needs a per-instance
image, so that backend POSTs a raw ``Sandbox`` CR (no template) and execs via the Kubernetes pod-exec
API. The multiplication task uses **one fixed image**, so here we use the agent-sandbox Python SDK's
high-level API:

* spawn from a **SandboxTemplate** via ``SandboxClient.create_sandbox(template=...)``
  (optionally adopting from a warm pool), and
* execute via ``sandbox.commands.run(...)`` -> ``ExecutionResult(stdout, stderr, exit_code)``, which
  POSTs to the in-image ``:8888`` runtime server.

See ``docs/expansion-plan.md`` §2 and ``docs/agent-sandbox-research.md``.

**CAVEAT (blocker until set):** the SandboxTemplate's image MUST ship the agent-sandbox ``:8888``
runtime server — that's what ``commands.run`` talks to. See
``infra/manifests/sandbox-template-multiplication.yaml`` (image is a placeholder there).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from k8s_agent_sandbox.models import (
    SandboxInClusterConnectionConfig,
    SandboxLocalTunnelConnectionConfig,
)
from k8s_agent_sandbox.sandbox_client import SandboxClient


@dataclass
class MultiplicationSandboxConfig:
    """Knobs for the SDK-driven sandbox (overridable from the YAML ``environment:`` block)."""

    template: str = "multiplication-template"
    """SandboxTemplate name; must match infra/manifests/sandbox-template-multiplication.yaml."""
    namespace: str = "skyrl-sandboxes"
    warmpool: Optional[str] = None
    """Warm-pool policy (e.g. "default") once a WarmPool CR exists; None = cold create. See research doc."""
    sandbox_ready_timeout: int = 300
    command_timeout: int = 60
    in_cluster: bool = True
    """True: connect to the pod IP (a Ray worker running in-cluster). False: kubectl port-forward tunnel
    (a laptop / local-Ray run). Pod IPs are not routable from outside the cluster, so a laptop run MUST
    set this False."""
    server_port: int = 8888
    """The in-image runtime port ``commands.run`` POSTs to."""


class MultiplicationSandbox:
    """One agent-sandbox ``Sandbox`` driven entirely through the SDK (``create_sandbox`` + ``commands.run``)."""

    def __init__(self, config: MultiplicationSandboxConfig, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("skyrl_sandbox.multiplication.sandbox")
        if config.in_cluster:
            conn = SandboxInClusterConnectionConfig(use_pod_ip=True, server_port=config.server_port)
        else:
            conn = SandboxLocalTunnelConnectionConfig(server_port=config.server_port)
        # SandboxClient loads in/out-of-cluster k8s config itself (via its K8sHelper).
        self._client = SandboxClient(connection_config=conn)
        self._sandbox = None  # the SDK Sandbox handle

    @property
    def claim_name(self) -> Optional[str]:
        return self._sandbox.claim_name if self._sandbox is not None else None

    def start(self) -> None:
        """Spawn the sandbox from the template (Claim -> Template -> Sandbox), wait Ready."""
        self._sandbox = self._client.create_sandbox(
            template=self.config.template,
            namespace=self.config.namespace,
            sandbox_ready_timeout=self.config.sandbox_ready_timeout,
            warmpool=self.config.warmpool,
        )
        self.logger.info("multiplication sandbox ready (claim=%s)", self._sandbox.claim_name)

    def run(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        """Run ``command`` via the SDK's commands.run (in-image :8888 runtime). Returns a plain dict."""
        assert self._sandbox is not None, "sandbox not started"
        result = self._sandbox.commands.run(command, timeout=timeout or self.config.command_timeout)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.exit_code}

    def delete(self) -> None:
        """Delete the sandbox (idempotent; never raises)."""
        if self._sandbox is None:
            return
        try:
            self._client.delete_sandbox(self._sandbox.claim_name, namespace=self.config.namespace)
        except Exception as e:  # never raise from cleanup
            self.logger.warning("failed to delete sandbox %s: %s", self._sandbox.claim_name, e)
        finally:
            self._sandbox = None
