"""A mini-swe-agent ``Environment`` backed by kubernetes-sigs/agent-sandbox.

Drop-in replacement for mini-swe-agent's ``DockerEnvironment``: instead of ``podman run`` +
``podman exec`` on the local host, each SWE-bench box is an agent-sandbox ``Sandbox`` pod on a
Kubernetes cluster, and commands run via the Kubernetes **pod-exec** API.

Selected purely by config via mini-swe-agent's dotted-path env factory (no SkyRL/mini-swe-agent
source changes)::

    environment:
      environment_class: "mini_swe_agent_sandbox.environment.AgentSandboxEnvironment"

Design (see docs/port-plan.md and docs/port-guided.md):

* **Strategy A — direct ``Sandbox`` CR + pod-exec.** The agent-sandbox Python SDK creates sandboxes
  from a *named* ``SandboxTemplate`` and execs over an in-image HTTP server on ``:8888``; SWE-bench
  images have no such server and need a *per-instance image* the SDK can't set. So we create the
  ``Sandbox`` CR directly (inline ``podTemplate`` with the per-instance image) and exec with the
  pod-exec API (the direct analogue of ``docker exec``). GVK/fields match this repo's
  ``infra/manifests/sandbox-example.yaml`` (``agents.x-k8s.io/v1beta1``).
* **Security.** The sandbox pod runs untrusted, model-generated bash, so it gets **no** API token
  (``automountServiceAccountToken: false``) and is pinned to the gVisor pool. The Kubernetes identity
  (RBAC) belongs to the Ray worker that *drives* the sandbox, not the sandbox itself
  (see ``infra/05-setup-rbac.sh``).
* **Contract.** mini-swe-agent 1.x calls ``execute(command: str, ...) -> {"output", "returncode"}``.
  We mirror that, and accept the 2.x ``execute(action: dict)`` form defensively. The RL reward is the
  eval script's ``returncode == 0``; pod-exec does not return the exit code in-band like
  ``docker exec``, so we recover it from the exec error channel and never silently map a failure to 0.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes import watch as k8s_watch
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream as k8s_stream
    from kubernetes.stream.ws_client import ERROR_CHANNEL
except ImportError as e:  # pragma: no cover - dependency guard
    raise ImportError(
        "AgentSandboxEnvironment requires the official Kubernetes client: `pip install kubernetes`."
    ) from e

# Sandbox CRD coordinates. Group/plural/annotation are stable; the served version is pinned here to
# match this repo's infra (infra/manifests/sandbox-example.yaml uses v1beta1). Note the
# k8s-agent-sandbox SDK's constants currently say v1alpha1 -- we intentionally do NOT depend on them
# (they lag the deployed CRD, and the SDK's high-level client can't set a per-instance image).
SANDBOX_API_GROUP = "agents.x-k8s.io"
SANDBOX_PLURAL = "sandboxes"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


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
    api_version: str = "v1beta1"
    """Served version of the Sandbox CRD (this repo's infra uses v1beta1)."""
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

    labels: dict[str, str] = field(default_factory=dict)
    """Extra labels (merged with ``app=mini-swe-agent-sandbox``) for GC/blast-radius."""

    sandbox_ready_timeout: int = 600
    """Seconds to wait for the Sandbox to report Ready (large SWE-bench images: pull + schedule)."""
    shutdown_after_seconds: int = 0
    """Optional TTL backstop. 0 = off. When >0 sets ``spec.lifecycle`` (verify your CRD supports it on
    a bare Sandbox). Explicit ``cleanup()`` is the primary reaper."""


def _lifecycle_spec(shutdown_after_seconds: int) -> dict[str, str]:
    """Best-effort TTL, mirroring the SDK's claim lifecycle shape (now+N, UTC, Delete)."""
    shutdown_time = datetime.now(timezone.utc) + timedelta(seconds=shutdown_after_seconds)
    return {"shutdownTime": shutdown_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "shutdownPolicy": "Delete"}


def _parse_exec_returncode(error_channel: str) -> int:
    """Recover the command exit code from a pod-exec ERROR_CHANNEL status message.

    The Kubernetes exec API reports completion as a v1.Status JSON on the error channel:
    ``{"status": "Success"}`` (rc 0) or ``{"status": "Failure", "details": {"causes":
    [{"reason": "ExitCode", "message": "<n>"}]}}``. A failed/garbled status maps to nonzero (never a
    false 0) so an eval can't be silently marked resolved; an empty channel maps to 0 (the
    quiet-success case the k8s client itself assumes).
    """
    if not error_channel:
        return 0
    try:
        status = json.loads(error_channel)
    except (ValueError, TypeError):
        return 1
    if status.get("status") == "Success":
        return 0
    for cause in (status.get("details") or {}).get("causes", []):
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message"))
            except (TypeError, ValueError):
                return 1
    return 1


class AgentSandboxEnvironment:
    """mini-swe-agent ``Environment`` whose sandbox is an agent-sandbox ``Sandbox`` pod."""

    def __init__(
        self,
        *,
        config_class: type = AgentSandboxEnvironmentConfig,
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        self.logger = logger or logging.getLogger("mini_swe_agent_sandbox.environment")

        # Filter unknown keys so an unrelated YAML field doesn't crash construction.
        known = {f.name for f in dataclasses.fields(config_class)}
        ignored = set(kwargs) - known
        if ignored:
            self.logger.debug("AgentSandboxEnvironment ignoring unknown config keys: %s", sorted(ignored))
        self.config: AgentSandboxEnvironmentConfig = config_class(**{k: v for k, v in kwargs.items() if k in known})

        if not self.config.image:
            raise ValueError(
                "AgentSandboxEnvironment requires a non-empty `image`; it is normally injected "
                "per-instance by get_sb_environment() in mini_swe_agent_sandbox/utils.py."
            )

        # State (set defensively so a partially-constructed env cleans up safely).
        self._sandbox_name: Optional[str] = None
        self._pod_name: Optional[str] = None
        self._deleted: bool = False

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self._custom = k8s_client.CustomObjectsApi()
        self._core = k8s_client.CoreV1Api()

        self._start_sandbox()

    # --- lifecycle ---------------------------------------------------------------------------

    def _start_sandbox(self) -> None:
        name = f"mswe-{uuid.uuid4().hex[:8]}"
        container: dict[str, Any] = {
            "name": self.config.container_name,
            "image": self.config.image,
            "command": list(self.config.container_command),
            "workingDir": self.config.cwd,
            "imagePullPolicy": self.config.image_pull_policy,
        }
        if self.config.env:
            container["env"] = [{"name": k, "value": str(v)} for k, v in self.config.env.items()]
        if self.config.resources:
            container["resources"] = self.config.resources

        pod_spec: dict[str, Any] = {
            "containers": [container],
            "restartPolicy": self.config.restart_policy,
            # SECURITY: untrusted model code never holds an API credential.
            "automountServiceAccountToken": self.config.automount_service_account_token,
        }
        if self.config.runtime_class_name:
            pod_spec["runtimeClassName"] = self.config.runtime_class_name
        if self.config.node_selector:
            pod_spec["nodeSelector"] = dict(self.config.node_selector)
        if self.config.tolerations:
            pod_spec["tolerations"] = [dict(t) for t in self.config.tolerations]
        if self.config.service_account:
            pod_spec["serviceAccountName"] = self.config.service_account

        spec: dict[str, Any] = {"podTemplate": {"spec": pod_spec}}
        if self.config.shutdown_after_seconds and self.config.shutdown_after_seconds > 0:
            spec["lifecycle"] = _lifecycle_spec(self.config.shutdown_after_seconds)

        labels = {"app": "mini-swe-agent-sandbox"}
        labels.update(self.config.labels or {})

        manifest = {
            "apiVersion": f"{SANDBOX_API_GROUP}/{self.config.api_version}",
            "kind": "Sandbox",
            "metadata": {"name": name, "labels": labels},
            "spec": spec,
        }

        self.logger.info("Creating Sandbox '%s' in ns '%s' (image=%s)", name, self.config.namespace, self.config.image)
        self._custom.create_namespaced_custom_object(
            group=SANDBOX_API_GROUP,
            version=self.config.api_version,
            namespace=self.config.namespace,
            plural=SANDBOX_PLURAL,
            body=manifest,
        )
        # A CR now exists and must be cleaned up even if readiness fails.
        self._sandbox_name = name
        try:
            self._wait_ready(name, self.config.sandbox_ready_timeout)
            self._pod_name = self._resolve_pod_name(name)
            self.logger.info("Sandbox '%s' ready (pod=%s)", name, self._pod_name)
        except Exception:
            self.cleanup()
            raise

    def _wait_ready(self, name: str, timeout: int) -> None:
        """Watch the Sandbox CR until ``status.conditions[Ready]==True``."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError(f"Sandbox '{name}' did not become ready within {timeout}s.")
            w = k8s_watch.Watch()
            for event in w.stream(
                self._custom.list_namespaced_custom_object,
                group=SANDBOX_API_GROUP,
                version=self.config.api_version,
                namespace=self.config.namespace,
                plural=SANDBOX_PLURAL,
                field_selector=f"metadata.name={name}",
                timeout_seconds=remaining,
            ):
                if not event:
                    continue
                if event.get("type") == "DELETED":
                    w.stop()
                    raise RuntimeError(f"Sandbox '{name}' was deleted before becoming ready.")
                if event.get("type") in ("ADDED", "MODIFIED"):
                    status = event["object"].get("status") or {}
                    for cond in status.get("conditions", []):
                        if cond.get("type") == "Ready" and cond.get("status") == "True":
                            w.stop()
                            return

    def _resolve_pod_name(self, name: str) -> str:
        """Pod name from the Sandbox CR annotation; fall back to the sandbox name."""
        obj = self._get_sandbox(name) or {}
        annotations = (obj.get("metadata") or {}).get("annotations") or {}
        return annotations.get(POD_NAME_ANNOTATION) or name

    def _get_sandbox(self, name: str) -> Optional[dict]:
        try:
            return self._custom.get_namespaced_custom_object(
                group=SANDBOX_API_GROUP,
                version=self.config.api_version,
                namespace=self.config.namespace,
                plural=SANDBOX_PLURAL,
                name=name,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def cleanup(self) -> None:
        """Delete the Sandbox CR (the controller GCs the pod). Idempotent; safe in ``__del__``."""
        if self._sandbox_name is None or self._deleted:
            return
        self._deleted = True
        try:
            self._custom.delete_namespaced_custom_object(
                group=SANDBOX_API_GROUP,
                version=self.config.api_version,
                namespace=self.config.namespace,
                plural=SANDBOX_PLURAL,
                name=self._sandbox_name,
            )
            self.logger.info("Deleted Sandbox '%s'", self._sandbox_name)
        except ApiException as e:
            if e.status != 404:
                self.logger.warning("Failed to delete Sandbox '%s': %s", self._sandbox_name, e)
        except Exception as e:  # never raise from cleanup
            self.logger.warning("Failed to delete Sandbox '%s': %s", self._sandbox_name, e)

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # --- execution ---------------------------------------------------------------------------

    def execute(self, command: Any, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a bash command in the sandbox pod.

        Accepts a plain string (mini-swe-agent 1.x) or an ``{"command": ...}`` dict (2.x).
        Returns ``{"output": <merged stdout+stderr>, "returncode": <int>}`` (``-1`` on timeout/error).
        """
        cmd_str = command.get("command", "") if isinstance(command, dict) else command
        assert self._pod_name, "Sandbox pod not started"

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

        return self._pod_exec(["bash", "-lc", script], timeout or self.config.timeout)

    def _pod_exec(self, argv: list[str], timeout: int) -> dict[str, Any]:
        try:
            resp = k8s_stream(
                self._core.connect_get_namespaced_pod_exec,
                self._pod_name,
                self.config.namespace,
                command=argv,
                container=self.config.container_name,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except ApiException as e:
            return {"output": f"pod exec failed to start: {e}", "returncode": -1}

        chunks: list[str] = []
        start = time.monotonic()
        error_channel = ""
        try:
            while resp.is_open():
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    return {"output": "".join(chunks) + f"\n<command timed out after {timeout}s>", "returncode": -1}
                resp.update(timeout=min(1.0, max(0.1, timeout - elapsed)))
                if resp.peek_stdout():
                    chunks.append(resp.read_stdout())
                if resp.peek_stderr():
                    chunks.append(resp.read_stderr())
            error_channel = resp.read_channel(ERROR_CHANNEL)
        finally:
            resp.close()

        return {"output": "".join(chunks), "returncode": _parse_exec_returncode(error_channel)}

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
