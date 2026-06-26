"""Kubernetes / agent-sandbox plumbing for :class:`AgentSandboxEnvironment`.

Everything that talks to the Kubernetes API or the agent-sandbox Python SDK (``k8s_agent_sandbox``)
is confined to this module, so ``environment.py`` stays a thin mini-swe-agent ``Environment``
adapter. The single public class, :class:`KubernetesSandbox`, owns one ``Sandbox`` CR's full
lifecycle::

    create the Sandbox CR  →  wait for Ready  →  resolve its pod  →  pod-exec  →  delete the CR

Design (rationale in docs/expansion-plan.md):

* **Uses the agent-sandbox SDK for lifecycle, not spawn-by-template.** We build on the SDK's
  ``K8sHelper`` (its in/out-of-cluster config + API clients + tested readiness watch) and its
  ``constants`` so the Sandbox GVK always tracks the installed CRD (currently
  ``agents.x-k8s.io/v1beta1``) instead of being hard-coded. We create the ``Sandbox`` CR
  **directly** via the SDK's API client rather than ``SandboxClient.create_sandbox(warmpool=...)``,
  because that spawns from a *named SandboxWarmPool → SandboxTemplate* (single image) and can't carry
  the per-instance SWE-bench image.
* **Exec → Kubernetes pod-exec** (the ``docker exec`` analogue), not the SDK's ``commands.run``:
  ``commands.run`` POSTs to an in-image HTTP server on ``:8888`` that SWE-bench images don't ship.
  pod-exec does not return the exit code in-band like ``docker exec``, so we recover it from the
  exec error channel and never silently map a failure to 0 (the RL reward is the eval script's
  ``returncode == 0``).
* **Security.** The sandbox pod runs untrusted, model-generated bash, so it gets **no** API token
  (``automountServiceAccountToken: false``) and is pinned to the gVisor pool. The Kubernetes
  identity (RBAC) belongs to the Ray worker that *drives* the sandbox, not the sandbox itself.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream
from kubernetes.stream.ws_client import ERROR_CHANNEL

# Lifecycle + GVK come from the agent-sandbox SDK so they track the installed CRD/controller.
from k8s_agent_sandbox.constants import (
    POD_NAME_ANNOTATION,
    SANDBOX_API_GROUP,
    SANDBOX_API_VERSION,
    SANDBOX_PLURAL_NAME,
)
from k8s_agent_sandbox.k8s_helper import K8sHelper


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


class KubernetesSandbox:
    """Lifecycle + exec for one agent-sandbox ``Sandbox`` pod, via the Kubernetes API.

    Construct it with the resolved environment config — it reads the Kubernetes/placement fields
    (``image``, ``cwd``, ``env``, ``namespace``, ``container_*``, ``resources``, the isolation knobs,
    and the timeouts) — then call :meth:`start`. All Kubernetes / agent-sandbox-SDK access is
    confined to this class; the caller only assembles the bash to run and interprets the result.
    """

    def __init__(self, config: Any, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("skyrl_sandbox.mini_swe_agent.kubernetes_util")

        # State (set defensively so a partially-constructed sandbox deletes safely).
        self.sandbox_name: Optional[str] = None
        self.pod_name: Optional[str] = None
        self._deleted: bool = False

        # SDK helper: loads in-cluster config (falls back to kubeconfig) and exposes the API clients
        # + a tested readiness watch. We use its clients directly for create/delete (the high-level
        # SandboxClient.create_sandbox can't set a per-instance image).
        self._helper = K8sHelper()
        self._custom = self._helper.custom_objects_api
        self._core = self._helper.core_v1_api

    # --- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        """Create the Sandbox CR, wait for Ready, and resolve its pod. Cleans up on failure."""
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
            "apiVersion": f"{SANDBOX_API_GROUP}/{SANDBOX_API_VERSION}",
            "kind": "Sandbox",
            "metadata": {"name": name, "labels": labels},
            "spec": spec,
        }

        self.logger.info("Creating Sandbox '%s' in ns '%s' (image=%s)", name, self.config.namespace, self.config.image)
        self._custom.create_namespaced_custom_object(
            group=SANDBOX_API_GROUP,
            version=SANDBOX_API_VERSION,
            namespace=self.config.namespace,
            plural=SANDBOX_PLURAL_NAME,
            body=manifest,
        )
        # A CR now exists and must be cleaned up even if readiness fails.
        self.sandbox_name = name
        try:
            # SDK's tested watch for status.conditions[Ready]==True (raises on timeout/deletion).
            self._helper.wait_for_sandbox_ready(name, self.config.namespace, self.config.sandbox_ready_timeout)
            self.pod_name = self._resolve_pod_name(name)
            self.logger.info("Sandbox '%s' ready (pod=%s)", name, self.pod_name)
        except Exception:
            self.delete()
            raise

    def _resolve_pod_name(self, name: str) -> str:
        """Pod name from the Sandbox CR annotation; fall back to the sandbox name."""
        obj = self._helper.get_sandbox(name, self.config.namespace) or {}
        annotations = (obj.get("metadata") or {}).get("annotations") or {}
        return annotations.get(POD_NAME_ANNOTATION) or name

    def delete(self) -> None:
        """Delete the Sandbox CR (the controller GCs the pod). Idempotent; safe in ``__del__``."""
        if self.sandbox_name is None or self._deleted:
            return
        self._deleted = True
        try:
            self._custom.delete_namespaced_custom_object(
                group=SANDBOX_API_GROUP,
                version=SANDBOX_API_VERSION,
                namespace=self.config.namespace,
                plural=SANDBOX_PLURAL_NAME,
                name=self.sandbox_name,
            )
            self.logger.info("Deleted Sandbox '%s'", self.sandbox_name)
        except ApiException as e:
            if e.status != 404:
                self.logger.warning("Failed to delete Sandbox '%s': %s", self.sandbox_name, e)
        except Exception as e:  # never raise from delete
            self.logger.warning("Failed to delete Sandbox '%s': %s", self.sandbox_name, e)

    # --- execution ---------------------------------------------------------------------------

    def exec(self, argv: list[str], timeout: int) -> dict[str, Any]:
        """Run ``argv`` in the sandbox pod via the Kubernetes pod-exec API.

        Returns ``{"output": <merged stdout+stderr>, "returncode": <int>}`` (``-1`` on
        timeout/transport error).
        """
        assert self.pod_name, "Sandbox pod not started"
        try:
            resp = k8s_stream(
                self._core.connect_get_namespaced_pod_exec,
                self.pod_name,
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
