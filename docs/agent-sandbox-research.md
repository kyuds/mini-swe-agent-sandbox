# agent-sandbox research notes

Answers to two questions about kubernetes-sigs/agent-sandbox, grounded in the installed SDK
(`k8s-agent-sandbox==0.5.0`, pinned `>=0.5.0,<0.6`) source (`sandbox_client.py`, `k8s_helper.py`,
`constants.py`, `models.py`) plus the upstream v1beta1 manifests in `extensions/examples/`.

> **0.5.0 changed the model** vs 0.4.x: GVK moved `v1alpha1` → **`v1beta1`**, and the SDK create path
> went from **template-based** (`create_sandbox(template=…)`, claim → template) to **warmpool-based**
> (`create_sandbox(warmpool=…)`, claim → **SandboxWarmPool** → SandboxTemplate). The chain is now:
> **SandboxClaim → SandboxWarmPool → SandboxTemplate → Sandbox.**

---

## Q1. Can you create a sandbox **without** a SandboxTemplate?

**Depends which layer — and it's the crux of why this repo has two backends.**

### SDK layer (`SandboxClient.create_sandbox`): **No — and in 0.5.0 you also need a warm pool.**
- `create_sandbox(warmpool: str, …)` raises `ValueError("Warmpool name cannot be empty.")` if
  `warmpool` is falsy. There is **no `template` argument anymore** — the template is reached indirectly.
- It writes a **`SandboxClaim`** with `spec.warmPoolRef.name = <warmpool>` (`k8s_helper.create_sandbox_claim`);
  the controller resolves that claim against the named **`SandboxWarmPool`**, whose
  `spec.sandboxTemplateRef.name` points at the **`SandboxTemplate`** that carries the image.
- Consequence: the image lives *only* in the template, reached via the pool. `create_sandbox` has **no
  per-instance image parameter**. N different images ⇒ N templates (and N pools).

### CRD layer (core `Sandbox` resource): **Yes — no template, no warm pool needed.**
- You can POST a core `Sandbox` CR directly with an **inline `spec.podTemplate`** carrying the image —
  no `SandboxTemplate`, `SandboxWarmPool`, or `SandboxClaim`. This is exactly what this repo's
  **mini-swe-agent backend** does: `KubernetesSandbox.start()` calls
  `custom_objects_api.create_namespaced_custom_object(group="agents.x-k8s.io", version="v1beta1",
  plural="sandboxes", body=<inline manifest>)` — GVK read from the SDK `constants` so it tracks the
  installed CRD (see `skyrl_sandbox/mini_swe_agent/kubernetes_util.py`).
- Trade-off: a bare `Sandbox` you created yourself gives you **no SDK handle**, so no
  `sandbox.commands.run()` / `sandbox.files` (those need the in-image `:8888` runtime *and* a
  `SandboxConnectionConfig`). You exec via the Kubernetes **pod-exec** API instead.

### So the two layers map cleanly onto the two examples
| | image model | create via | exec via | needs `:8888` runtime image? |
|---|---|---|---|---|
| **mini-swe-agent** (SWE-bench) | per-instance (thousands) | raw `Sandbox` CR (no template/pool) | pod-exec | no |
| **multiplication** (toy) | one fixed image | SDK `create_sandbox(warmpool=…)` → pool → template | `commands.run` | **yes** |

---

## Q2. Does agent-sandbox support a **fixed pool** serving a bunch of **distinct images**?

**No — a warm pool is single-image by construction.** In 0.5.0 it's also no longer optional: the SDK
create path *requires* a pool, so the multiplication example uses one; SWE-bench still can't.

### The mechanism: SandboxWarmPool (now the required spawn unit for the SDK)
- A **`SandboxWarmPool`** (extensions CRD `extensions.agents.x-k8s.io/v1beta1`) **pre-provisions**
  `spec.replicas` pods from a single `spec.sandboxTemplateRef`, so an incoming claim **adopts** a ready
  one instead of cold-starting. The SDK notes adoption explicitly: *"With warm pool adoption, the
  sandbox name may differ from the claim name"* (`k8s_helper.resolve_sandbox_name`).
- `create_sandbox(warmpool=…)` is the only SDK create entry point (0.5.0): you *must* name a pool.

### Why it's inherently one-image-per-pool
Pre-warming means the pod is **already running a specific image** before any claim arrives — you can't
pre-warm a pod for an image you don't yet know. A pool references **one `SandboxTemplate`** = **one
image** (`sandboxTemplateRef` is a single name). There is no per-claim image override (the claim only
carries `warmPoolRef.name` + optional `additionalPodMetadata`). So **one pool = one image**; a single
pool cannot serve heterogeneous images.

### Implications for this repo
- **SWE-bench / mini-swe-agent (thousands of distinct per-instance images):** pools don't help — you'd
  need one pool per image (infeasible; can't pre-pull thousands of images). Independent reason the
  mini-swe backend uses **cold, per-instance raw `Sandbox` CRs** (image pulled on demand).
- **Multiplication (one fixed image):** a pool is the natural fit — and required by 0.5.0's SDK anyway.
  We ship `infra/manifests/sandbox-warmpool-multiplication.yaml` (`replicas: 2`,
  `sandboxTemplateRef: multiplication-template`); `replicas` = how many sandboxes stay warm, sized to
  peak concurrent trajectories.

### Caveat
The exact `SandboxWarmPool`/`SandboxTemplate` v1beta1 fields here mirror `extensions/examples/` in the
agent-sandbox repo (`sandboxwarmpool.yaml`, `sandboxtemplate.yaml`); confirm against the
`extensions.yaml` your installed `AGENT_SANDBOX_VERSION` actually applies. The single-image-per-pool
conclusion is **high-confidence** (it follows from adoption semantics + the single `sandboxTemplateRef`).

---

## One-line answers
1. **create without a template?** Not via the SDK — 0.5.0's `create_sandbox(warmpool=…)` needs a warm
   pool, which needs a template. Template-less *and* pool-less only via a raw core `Sandbox` CR (what
   mini-swe does), at the cost of `commands.run`/`files` (must pod-exec instead).
2. **fixed pool for distinct images?** No — pools are single-image (one `sandboxTemplateRef` per pool).
   Required + natural for the single-image multiplication example; useless for SWE-bench's per-instance images.
