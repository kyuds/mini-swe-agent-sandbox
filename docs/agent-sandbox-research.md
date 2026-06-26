# agent-sandbox research notes

Answers to two questions about kubernetes-sigs/agent-sandbox, grounded in the installed SDK
(`k8s-agent-sandbox==0.4.6`) source. File refs are into that package
(`sandbox_client.py`, `k8s_helper.py`, `constants.py`, `models.py`).

---

## Q1. Can you create a sandbox **without** a SandboxTemplate?

**It depends which layer you mean — and the answer is the crux of why this repo has two backends.**

### SDK layer (`SandboxClient.create_sandbox`): **No — a template is mandatory.**
- `create_sandbox(template: str, ...)` raises `ValueError("Template name cannot be empty.")` if
  `template` is falsy (`sandbox_client.py:116-117`).
- It then creates a **`SandboxClaim`** whose `spec.sandboxTemplateRef.name = <template>`
  (`k8s_helper.py:52-56`); the controller resolves that claim into a `Sandbox` stamped from the
  named template. The entire high-level path is template-centric: **Claim → Template → Sandbox**.
- Consequence: the image lives *only* in the template. `create_sandbox` has **no per-instance image
  parameter**. To use it for N different images you need N templates.

### CRD layer (core `Sandbox` resource): **Yes — no template needed.**
- You can POST a core `Sandbox` CR directly with an **inline `spec.podTemplate`** carrying the image
  and pod spec — no `SandboxTemplate`, no `SandboxClaim`. This is exactly what this repo's
  **mini-swe-agent backend** does: `KubernetesSandbox.start()` calls
  `custom_objects_api.create_namespaced_custom_object(group="agents.x-k8s.io", version="v1alpha1",
  plural="sandboxes", body=<inline manifest>)` (see `skyrl_sandbox/mini_swe_agent/kubernetes_util.py`).
- Trade-off: a bare `Sandbox` you created yourself gives you **no SDK handle**, so you don't get
  `sandbox.commands.run()` / `sandbox.files` (those require the in-image `:8888` runtime *and* a
  `SandboxConnectionConfig`). You exec via the Kubernetes **pod-exec** API instead.

### So the two layers map cleanly onto the two examples in this repo
| | image model | create via | exec via | needs `:8888` runtime image? |
|---|---|---|---|---|
| **mini-swe-agent** (SWE-bench) | per-instance (thousands) | raw `Sandbox` CR (no template) | pod-exec | no |
| **multiplication** (toy) | one fixed image | SDK `create_sandbox(template=…)` | `commands.run` | **yes** |

---

## Q2. Does agent-sandbox support a **fixed pool** serving a bunch of **distinct images**?

**No — a warm pool is single-image by construction.** It helps the multiplication example, not SWE-bench.

### The mechanism: WarmPool + the claim-side `warmpool` policy
- `create_sandbox(..., warmpool: str | None)` — a *policy string* (`"default"`, `"none"`, or a custom
  pool name; `sandbox_client.py:94,103`). It's written to the claim as `spec.warmpool`
  (`k8s_helper.py:59-60`).
- A **WarmPool** (an extensions CRD, installed by `extensions.yaml` — same bundle as
  SandboxTemplate/SandboxClaim) **pre-provisions sandbox pods** so an incoming claim can **adopt** a
  ready one instantly instead of cold-starting. The SDK acknowledges adoption explicitly: *"With warm
  pool adoption, the sandbox name may differ from the claim name"* (`k8s_helper.py:79`,
  `resolve_sandbox_name` watches the claim status for the adopted Sandbox's name).

### Why it's inherently one-image-per-pool
Pre-warming means the pod is **already running a specific image** before any claim arrives. You cannot
pre-warm a pod for an image you don't yet know. A warm pool is provisioned from **one
SandboxTemplate** = **one image**. There is no per-claim image override anywhere in the SDK (the
claim only carries a `sandboxTemplateRef.name` + the `warmpool` string). So **one pool = one image**;
a single pool cannot serve heterogeneous images.

### Implications for this repo
- **SWE-bench / mini-swe-agent (thousands of distinct per-instance images):** warm pools do **not**
  help. You'd need one pool per image (infeasible — you can't pre-pull thousands of images, and the
  whole point of pooling evaporates). This is an independent reason the mini-swe backend uses
  **cold, per-instance `Sandbox` CRs** (image pulled on demand at create time).
- **Multiplication (one fixed image):** a warm pool is a **great fit** — pre-warm K pods of the
  single image so each trajectory adopts a ready sandbox, removing per-trajectory cold-start
  (schedule + image pull + `:8888` boot) from the hot path. Wiring is just
  `create_sandbox(template="multiplication-template", warmpool="default")` once a `WarmPool` CR for
  that template exists.

### Caveat (verify on cluster)
The SDK only exposes the **claim-side** `warmpool` *string*; it does not define or create the
`WarmPool` CR. The exact WarmPool CRD schema (fields, replica count, and the assumed 1:1 binding to a
single template) should be confirmed against the installed `extensions.yaml` on the target cluster
before relying on it. Confidence is **high** on the single-image-per-pool conclusion (it follows from
the adoption semantics); the unverified part is only the precise CRD field layout. For the initial
multiplication example we will **not** depend on warm pools — plain `create_sandbox(template=…)` is
enough; a warm pool is a documented latency optimization to add later.

---

## One-line answers
1. **create without a template?** Not via the SDK's `create_sandbox` (template mandatory) — but yes
   via a raw core `Sandbox` CR with an inline podTemplate (what the mini-swe backend does), at the
   cost of `commands.run`/`files` (must pod-exec instead).
2. **fixed pool for distinct images?** No — warm pools are single-image (one template per pool). Great
   for the single-image multiplication example; useless for SWE-bench's per-instance images.
