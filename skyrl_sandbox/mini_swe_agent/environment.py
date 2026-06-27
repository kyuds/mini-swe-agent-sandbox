"""A mini-swe-agent ``Environment`` backed by kubernetes-sigs/agent-sandbox.

Drop-in replacement for SkyRL mini-swe-agent example's ``DockerEnvironment``: instead of
``podman run`` + ``podman exec`` on the local host, each SWE-bench box is an agent-sandbox
``Sandbox`` pod on a Kubernetes cluster, and commands run via the Kubernetes **pod-exec** API.

Selected purely by config via mini-swe-agent's dotted-path env factory (no SkyRL/mini-swe-agent
source changes)::

    environment:
      environment_class: "skyrl_sandbox.mini_swe_agent.environment.AgentSandboxEnvironment"

This module is the **mini-swe-agent adapter**: it owns the config schema and the ``Environment``
protocol (``execute``/``cleanup``/``serialize``). All Kubernetes + agent-sandbox-SDK plumbing —
creating the ``Sandbox`` CR, waiting for Ready, pod-exec, deletion — lives in :mod:`kubernetes_util`
(:class:`~skyrl_sandbox.mini_swe_agent.kubernetes_util.KubernetesSandbox`).

* **Contract.** mini-swe-agent 1.x calls ``execute(command: str, ...) -> {"output", "returncode"}``.
  We mirror that, and accept the 2.x ``execute(action: dict)`` form defensively. The RL reward is the
  eval script's ``returncode == 0``, so the exec path (in :mod:`kubernetes_util`) never silently maps
  a failure to 0.
* **Security.** The sandbox pod runs untrusted, model-generated bash, so it gets **no** API token
  (``automountServiceAccountToken: false``) and is pinned to the gVisor pool. The Kubernetes identity
  (RBAC) belongs to the Ray worker that *drives* the sandbox, not the sandbox itself.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .kubernetes_util import KubernetesSandbox


@dataclass
class AgentSandboxEnvironmentConfig:
    """Keyword arguments accepted from the ``environment:`` block of the task YAML.

    Mirrors the relevant ``DockerEnvironmentConfig`` fields (image/cwd/env/forward_env/timeout) and
    adds Kubernetes/agent-sandbox placement + isolation knobs.
    """

    image: str = ""
    """Container image; injected per-instance by SkyRL's get_sb_environment (must be non-empty)."""
    cwd: str = "/testbed"
    """Working directory; set as the container ``workingDir`` so exec'd commands start here."""
    env: dict[str, str] = field(default_factory=dict)
    """Environment variables; baked into the pod's container ``env`` (≙ Docker backend ``-e``)."""
    forward_env: list[str] = field(default_factory=list)
    """Host (Ray-worker) env vars to forward; exported inline per-exec when set on the host."""
    timeout: int = 180
    """Default per-command timeout in seconds (eval passes a longer one explicitly)."""

    namespace: str = "skyrl-sandboxes"
    """Namespace for the Sandbox CRs + pods (matches SANDBOX_NAMESPACE in infra/.env)."""
    container_name: str = "sandbox"
    """Name of the single container in the sandbox pod (also the exec target)."""
    container_command: list[str] = field(default_factory=lambda: ["sleep", "infinity"])
    """Keeps the box idle so we can exec into it (≙ Docker backend's ``sleep 2h``)."""
    image_pull_policy: str = "IfNotPresent"
    resources: dict[str, Any] = field(
        default_factory=lambda: {
            "requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "8Gi"},
            "limits": {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "16Gi"},
        }
    )
    """k8s resource requests/limits. Unlike Ray's logical ``num_cpus``, these are scheduler-enforced."""

    # --- isolation / placement (defaults target this repo's gVisor sandbox pool) ---
    runtime_class_name: Optional[str] = "gvisor"
    """gVisor RuntimeClass for kernel isolation of untrusted bash. None to disable."""
    node_selector: dict[str, str] = field(default_factory=lambda: {"sandbox.gke.io/runtime": "gvisor"})
    """Pin sandbox pods to the GKE Sandbox (gVisor) node pool."""
    tolerations: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor", "effect": "NoSchedule"}
        ]
    )
    """Tolerate the gVisor node-pool taint."""
    restart_policy: str = "OnFailure"
    automount_service_account_token: bool = False
    """SECURITY: untrusted model-generated code must NOT receive a Kubernetes API token."""
    service_account: Optional[str] = None
    """Optional ``serviceAccountName`` for the sandbox pod. Leave ``None``: with
    ``automount_service_account_token=False`` the pod holds no API token regardless, and the RBAC
    identity that drives the sandbox belongs to the Ray worker, not the sandbox. Set only if your
    cluster requires a specific SA (e.g. for image pulls or PodSecurity admission)."""

    labels: dict[str, str] = field(default_factory=dict)
    """Extra labels (merged with ``app=mini-swe-agent-sandbox``) for GC/blast-radius."""

    sandbox_ready_timeout: int = 600
    """Seconds to wait for the Sandbox to report Ready (large SWE-bench images: pull + schedule)."""
    shutdown_after_seconds: int = 0
    """Optional TTL backstop. 0 = off. When >0 sets ``spec.lifecycle`` (verify your CRD supports it on
    a bare Sandbox). Explicit ``cleanup()`` is the primary reaper."""
    auto_cleanup: bool = True
    """When True, the sandbox is deleted automatically as the env is finalized (``__del__``) — the
    safety net that reaps a box if the caller forgets ``cleanup()``. Set False to let the sandbox
    outlive the process (e.g. the smoke test's ``--keep``, for manual ``kubectl exec`` inspection);
    an explicit ``cleanup()`` still deletes it regardless of this flag."""


class AgentSandboxEnvironment:
    """mini-swe-agent ``Environment`` whose sandbox is an agent-sandbox ``Sandbox`` pod."""

    def __init__(
        self,
        *,
        config_class: type = AgentSandboxEnvironmentConfig,
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        # Set first so cleanup()/__del__ are safe even if construction fails early.
        self._sandbox: Optional[KubernetesSandbox] = None
        self.logger = logger or logging.getLogger("skyrl_sandbox.mini_swe_agent.environment")

        # Filter unknown keys so an unrelated YAML field doesn't crash construction.
        known = {f.name for f in dataclasses.fields(config_class)}
        ignored = set(kwargs) - known
        if ignored:
            self.logger.debug("AgentSandboxEnvironment ignoring unknown config keys: %s", sorted(ignored))
        self.config: AgentSandboxEnvironmentConfig = config_class(**{k: v for k, v in kwargs.items() if k in known})

        if not self.config.image:
            raise ValueError(
                "AgentSandboxEnvironment requires a non-empty `image`; it is normally injected "
                "per-instance by get_sb_environment() in skyrl_sandbox/mini_swe_agent/utils.py."
            )

        # All Kubernetes / agent-sandbox interaction is delegated to KubernetesSandbox.
        self._sandbox = KubernetesSandbox(self.config, self.logger)
        self._sandbox.start()

    # --- execution ---------------------------------------------------------------------------

    def execute(self, command: Any, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a bash command in the sandbox pod.

        Accepts a plain string (mini-swe-agent 1.x) or an ``{"command": ...}`` dict (2.x).
        Returns ``{"output": <merged stdout+stderr>, "returncode": <int>}`` (``-1`` on timeout/error).
        """
        cmd_str = command.get("command", "") if isinstance(command, dict) else command
        assert self._sandbox is not None, "Sandbox not started"

        # Common path is just `bash -lc <cmd>` (workingDir + env are set on the pod). A prelude is
        # only needed for the (unused-by-this-example) per-call cwd override or host env forwarding.
        prelude = ""
        for key in self.config.forward_env:
            value = os.getenv(key)
            if value is not None:
                prelude += f"export {key}={shlex.quote(value)}; "
        if cwd:
            prelude += f"cd {shlex.quote(cwd)} && "
        script = f"{prelude}{cmd_str}" if prelude else cmd_str

        return self._sandbox.exec(["bash", "-lc", script], timeout or self.config.timeout)

    # --- lifecycle ---------------------------------------------------------------------------

    def cleanup(self) -> None:
        """Delete the sandbox (the controller GCs the pod). Idempotent; safe in ``__del__``."""
        if self._sandbox is not None:
            self._sandbox.delete()

    def __del__(self):
        # Auto-reap on GC unless disabled (e.g. --keep wants the box to outlive the process).
        # getattr guard: construction may have failed before self.config was assigned.
        try:
            if getattr(self, "config", None) is None or self.config.auto_cleanup:
                self.cleanup()
        except Exception:
            pass

    # --- introspection -----------------------------------------------------------------------

    @property
    def sandbox_name(self) -> Optional[str]:
        """Name of the underlying ``Sandbox`` CR (``None`` until/unless started)."""
        return self._sandbox.sandbox_name if self._sandbox else None

    @property
    def pod_name(self) -> Optional[str]:
        """Name of the sandbox pod, i.e. the pod-exec target (``None`` until Ready)."""
        return self._sandbox.pod_name if self._sandbox else None

    # --- misc (mini-swe-agent Environment protocol) ------------------------------------------

    def get_template_vars(self) -> dict[str, Any]:
        return asdict(self.config)

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": asdict(self.config),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }
