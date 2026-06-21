"""Smoke test for AgentSandboxEnvironment against a live (CPU) agent-sandbox cluster.

Proves the agent-sandbox integration works **without SkyRL or GPUs**. It drives the env exactly the
way mini-swe-agent / SkyRL do — construct it through mini-swe-agent's `get_environment` factory
(the same dotted-path mechanism the YAML uses), then exercise the `execute(cmd) -> {output,
returncode}` contract and `cleanup()` — so a green run here is direct evidence that the SkyRL ↔
agent-sandbox path will work.

It can run from your laptop (uses your kubeconfig; pod-exec is proxied by the API server) or
in-cluster. The only cluster requirement is the agent-sandbox controller + CRDs (infra steps 01 + 04);
the GPU pool and KubeRay are NOT needed.

    uv run python scripts/smoke_test_agent_sandbox.py --namespace default

Useful flags:
    --image IMG          container image (default python:3.11-slim; any image with bash works)
    --namespace NS       namespace for the Sandbox (default skyrl-sandboxes; use one you can write to)
    --cwd DIR            working dir (default /tmp; SWE-bench images use /testbed)
    --gvisor             pin to the gVisor pool (only if infra step 02 sandbox pool exists)
    --keep               leave the Sandbox running at the end for manual `kubectl exec` inspection
"""

import argparse
import sys
import time

# Independent Kubernetes client (the agent-sandbox SDK's helper) used only to confirm CR deletion at
# the end — querying separately from the env that did the delete is the more honest GC check.
from k8s_agent_sandbox.k8s_helper import K8sHelper

# Construct via mini-swe-agent's factory when available (exercises the real dotted-path wiring);
# fall back to importing the class directly so the test still runs without mini-swe-agent installed.
try:
    from minisweagent.environments import get_environment

    _HAVE_FACTORY = True
except Exception:  # pragma: no cover
    from mini_swe_agent_sandbox.environment import AgentSandboxEnvironment

    _HAVE_FACTORY = False

ENV_CLASS = "mini_swe_agent_sandbox.environment.AgentSandboxEnvironment"

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def make_env(config: dict):
    """Build the env the way SkyRL does: via get_environment (dotted-path) if available."""
    if _HAVE_FACTORY:
        print(f"  constructing via minisweagent.get_environment(environment_class={ENV_CLASS!r})")
        return get_environment(dict(config, environment_class=ENV_CLASS))
    print("  minisweagent not installed; constructing AgentSandboxEnvironment directly")
    return AgentSandboxEnvironment(**config)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="python:3.11-slim")
    ap.add_argument("--namespace", default="skyrl-sandboxes")
    ap.add_argument("--cwd", default="/tmp")
    ap.add_argument("--gvisor", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    # gVisor is OFF by default so the pod schedules on any vanilla CPU node. Modest resources so it
    # fits small nodes / Autopilot.
    isolation = (
        {"runtime_class_name": "gvisor"}
        if args.gvisor
        else {"runtime_class_name": None, "node_selector": {}, "tolerations": []}
    )
    config = {
        "image": args.image,
        "cwd": args.cwd,
        "namespace": args.namespace,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"}},
        "automount_service_account_token": False,
        "sandbox_ready_timeout": 300,
        **isolation,
    }

    print(f"\n== Phase 1: create Sandbox + wait Ready (ns={args.namespace}, image={args.image}) ==")
    try:
        env = make_env(config)
    except Exception as e:  # give actionable hints for the common failures
        print(f"\nFAILED to create/ready the Sandbox: {type(e).__name__}: {e}\n")
        msg = str(e).lower()
        if "could not find" in msg or "404" in msg or "not found" in msg:
            print("  → Is the agent-sandbox controller + CRD installed? (infra/04-install-agent-sandbox.sh)")
            print("  → The Sandbox GVK comes from the k8s-agent-sandbox SDK; if the installed CRD serves a")
            print("    different api version, install a matching SDK version.")
        elif "forbidden" in msg or "403" in msg:
            print(
                "  → RBAC: your identity can't create sandboxes/pods/exec in this namespace (infra/05-setup-rbac.sh)."
            )
        elif "did not become ready" in msg:
            print(f"  → Pod never Ready. Inspect: kubectl -n {args.namespace} get pods,sandboxes")
            print("  → If you passed --gvisor, ensure the gVisor node pool exists (infra step 02).")
        return 1
    check("sandbox created + Ready", env.pod_name is not None, f"sandbox={env.sandbox_name} pod={env.pod_name}")

    try:
        print("\n== Phase 2: basic execute() ==")
        r = env.execute("echo hello-from-sandbox")
        check("stdout captured", "hello-from-sandbox" in r["output"], r["output"].strip())
        check("returncode 0 on success", r["returncode"] == 0, f"rc={r['returncode']}")
        check("contract shape {output,returncode}", {"output", "returncode"} <= set(r), str(sorted(r)))

        print("\n== Phase 3: exit-code fidelity (the RL-reward-critical bit) ==")
        check("true -> 0", env.execute("true")["returncode"] == 0)
        check("false -> 1", env.execute("false")["returncode"] == 1)
        check("exit 7 -> 7", env.execute("exit 7")["returncode"] == 7)
        rr = env.execute("echo oops >&2; exit 3")
        check(
            "stderr merged + rc 3",
            "oops" in rr["output"] and rr["returncode"] == 3,
            f"rc={rr['returncode']} out={rr['output'].strip()!r}",
        )

        print("\n== Phase 4: cwd + filesystem persistence across execs (agent-loop pattern) ==")
        check(
            "cwd is the configured dir",
            env.execute("pwd")["output"].strip() == args.cwd,
            env.execute("pwd")["output"].strip(),
        )
        env.execute("echo persisted > smoke_state.txt")  # relative to cwd
        check("file persists into a later exec", env.execute("cat smoke_state.txt")["output"].strip() == "persisted")
        env.execute("export SMOKE_VAR=fromprev")  # runs in its own subshell
        shell_state = env.execute('echo "${SMOKE_VAR:-unset}"')["output"].strip()
        check("shell env does NOT persist across execs (fresh subshell)", shell_state == "unset", shell_state)

        print("\n== Phase 5: heredoc eval-script form (exactly how evaluate_trajectory runs the eval) ==")
        eval_ok = "set -e\necho running\ntest 2 -eq 2\necho done"
        r5 = env.execute(f"bash <<'EOF'\n{eval_ok}\nEOF")
        check("heredoc eval-script -> rc 0", r5["returncode"] == 0, r5["output"].strip().replace("\n", " | "))
        r5b = env.execute("bash <<'EOF'\necho will-fail\nexit 2\nEOF")
        check("heredoc eval-script propagates rc", r5b["returncode"] == 2, f"rc={r5b['returncode']}")

        # git apply heredoc (the literal SkyRL eval mechanism) — only if the image has git.
        if env.execute("command -v git >/dev/null 2>&1 && echo y")["output"].strip() == "y":
            setup = (
                "set -e\n"
                "rm -rf /tmp/repo && mkdir -p /tmp/repo && cd /tmp/repo\n"
                "git init -q && git config user.email a@b.c && git config user.name t\n"
                "printf 'line1\\n' > f.txt && git add -A && git commit -qm init"
            )
            env.execute(f"bash <<'EOF'\n{setup}\nEOF")
            patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n line1\n+line2"
            ap_res = env.execute(f"cd /tmp/repo && git apply <<'PATCH'\n{patch}\nPATCH")
            check(
                "git apply via heredoc -> rc 0",
                ap_res["returncode"] == 0,
                f"rc={ap_res['returncode']} {ap_res['output'].strip()!r}",
            )
            check("patch actually applied", "line2" in env.execute("cat /tmp/repo/f.txt")["output"])
        else:
            print("  (image has no git; skipped the git-apply sub-test — heredoc form already validated above)")

        print("\n== Phase 6: per-command timeout ==")
        r6 = env.execute("sleep 10", timeout=2)
        check("timeout -> rc -1 (no hang)", r6["returncode"] == -1, f"rc={r6['returncode']}")

        print("\n== Phase 7: dict action form (mini-swe-agent 2.x compatibility) ==")
        check(
            "execute({'command': ...}) works", env.execute({"command": "echo dict-ok"})["output"].strip() == "dict-ok"
        )

    finally:
        if args.keep:
            print("\n== Phase 8: cleanup SKIPPED (--keep) ==")
            print(f"  Sandbox '{env.sandbox_name}' / pod '{env.pod_name}' left running. Inspect with:")
            print(f"    kubectl -n {args.namespace} get sandbox {env.sandbox_name}")
            print(f"    kubectl -n {args.namespace} exec -it {env.pod_name} -- bash")
            print(f"    kubectl -n {args.namespace} delete sandbox {env.sandbox_name}   # when done")
        else:
            print("\n== Phase 8: cleanup() deletes the Sandbox CR ==")
            name = env.sandbox_name
            env.cleanup()
            helper = K8sHelper()  # independent client: confirm the CR is really gone
            gone = False
            for _ in range(30):  # allow the controller a moment to GC
                if helper.get_sandbox(name, args.namespace) is None:
                    gone = True
                    break
                time.sleep(1)
            check("Sandbox CR deleted (no leak)", gone, f"sandbox={name}")

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks: " + ", ".join(n for n, ok, _ in _results if not ok))
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
