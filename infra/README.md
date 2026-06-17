# infra/ — GKE cluster for mini-swe-agent + agent-sandbox

Provisions a GKE Standard cluster wired for SkyRL-style agentic RL: KubeRay drives
the training/inference cluster on GPUs, and `agent-sandbox` runs untrusted
SWE-bench command execution in gVisor-isolated pods. Everything is plain `bash` +
`gcloud`/`kubectl`/`helm` — no Terraform.

> **These scripts cost money** (GPU + gVisor nodes, and GPUs need quota). They are
> written to be read and run later; nothing here has been executed.

## Topology

```
GKE Standard cluster (one control plane, VPC-native → flat pod networking)
├─ default-pool          e2-standard-4  ×1     system pods + kuberay-operator + agent-sandbox controller
├─ kuberay-gpu-pool      g2/a3 + GPUs   0..N    Ray head/workers   (taint: workload=ray-gpu, + GKE GPU taint)
└─ sandbox-gvisor-pool   e2-standard-8  1..N    Sandbox pods       (gVisor; taint: sandbox.gke.io/runtime=gvisor)
```

The two operators are lightweight and stay on `default-pool` (they have no
tolerations, so the tainted pools reject them). Heavy workloads are pinned to
their pools by nodeSelector + tolerations.

## Files

| File | Requirement | What it does |
|------|-------------|--------------|
| `.env.example` | — | Configuration values (KEY=value). Copy to `.env` to customize. |
| `load-config.sh` | — | Loads `.env`/`.env.example` into the environment (no derivation). |
| `lib.sh` | — | Logging, command checks, idempotency guards. |
| `00-check-prerequisites.sh` | (5) | Verifies `gcloud`/`kubectl`/`helm`/`jq`/`gke-gcloud-auth-plugin`, auth, project, the GKE API, and prints GPU quota. |
| `01-create-cluster.sh` | (1) | Creates the cluster + small `default-pool`; fetches credentials. |
| `02-create-nodepools.sh` | (2)(3) | Adds `kuberay-gpu-pool` (GPUs, tainted) and `sandbox-gvisor-pool` (`--sandbox type=gvisor`). |
| `03-install-kuberay.sh` | (4) | Helm-installs the KubeRay operator, pinned to `default-pool`. |
| `04-install-agent-sandbox.sh` | (4) | `kubectl apply`s the agent-sandbox controller + Sandbox CRD (+ optional extensions). |
| `05-setup-rbac.sh` | (6) | Namespaces + ServiceAccount + Role + RoleBinding so Ray workers can CRUD Sandboxes and `exec`; verifies with `auth can-i`. |
| `06-verify.sh` | — | Read-only health check of pools, gVisor RuntimeClass, CRDs, operators, RBAC. |
| `07-deploy-raycluster.sh` | — | (optional) Render + apply the sample RayCluster; run by `up.sh` when `DEPLOY_RAYCLUSTER=1`. |
| `up.sh` / `teardown.sh` | — | Run all steps in order / delete the cluster. |
| `manifests/sandbox-example.yaml` | (7) | One **cold** gVisor Sandbox — validate the pool with a cheap CPU image, no GPU. |
| `manifests/raycluster-sample.yaml` | — | Sample RayCluster (head on default, GPU workers on the GPU pool), applied by `07`. |
| `manifests/kuberay-operator-values.yaml` | — | Helm values for the operator. |

## Usage

```bash
cd infra
cp .env.example .env                           # then edit .env — set PROJECT_ID, REGION, ZONE (gitignored)

./00-check-prerequisites.sh                    # fix anything it flags first
./up.sh                                         # runs 01..06 (+ 07 if DEPLOY_RAYCLUSTER=1)

# Validate gVisor cheaply (no GPU needed):
kubectl apply -f manifests/sandbox-example.yaml
kubectl -n skyrl-sandboxes get pods -w

./teardown.sh                                   # delete everything
```

No GPU quota yet? Build everything except the GPU pool and still exercise the
gVisor + sandbox + RBAC path cheaply:

```bash
SKIP_GPU_POOL=1 ./up.sh
```

Override defaults via env, e.g. an H100 box for the real run:

```bash
GPU_MACHINE=a3-highgpu-8g GPU_TYPE=nvidia-h100-80gb GPU_PER_NODE=8 \
ZONE=us-central1-a ./02-create-nodepools.sh
```

## (6) How "pods talking to each other" actually works

Two distinct paths — only one needs credentials:

- **Network (pod → pod IP / cluster DNS): automatic.** The cluster is VPC-native
  (`--enable-ip-alias`), so every pod is directly routable from every other pod
  across all node pools, with no tokens and no setup. This *is* the "direct
  pod-to-pod" path you asked about. (Node pools are a scheduling boundary, not a
  network boundary.)
- **Control plane (create/delete a Sandbox, `kubectl exec`): needs RBAC.** These
  are Kubernetes **API server** operations, so they require an identity. That
  identity is the Ray workers' ServiceAccount (`skyrl-sandbox-runner`); its
  auto-mounted projected token is the "service token." `05-setup-rbac.sh` grants
  it `sandboxes` CRUD + `pods/exec`, scoped to the sandbox namespace only.

  > There is no token-free way to create a CR or `exec` — those always traverse
  > the API server. The *only* RBAC-free command path is the agent-sandbox in-pod
  > HTTP server (`commands.run` on `:8888`), which needs that server baked into
  > the image. We deferred that (SWE-bench images don't ship it), so for now:
  > pod-exec + this RBAC.

Least privilege is asymmetric: the runner SA has powers; **sandbox pods get none**
and `automountServiceAccountToken: false` (untrusted model code must never hold an
API credential).

Point Ray pods at the identity with `serviceAccountName: skyrl-sandbox-runner`
(see `manifests/raycluster-sample.yaml`).

## Notes & deferrals

- **GKE Standard, zonal** by default (cheaper; GPUs are zone-specific). For HA,
  switch `--zone` → `--region` in `lib.sh:gcloud_location` and re-create.
- **gVisor forces ≥2 pools:** GKE won't let the only/default pool be gVisor
  (system pods need a normal pool) — so the split is required, not just tidy.
- **gVisor caveats:** no privileged pods, restricted `hostPath`, no GPUs (fine —
  sandboxes don't need them), some syscalls emulated. `git`/`pytest`/file edits
  are normally fine; validate a sample of SWE-bench instances before a full run.
- **(7) Cold sandboxes only.** No `SandboxTemplate`/`SandboxWarmPool` here. To cut
  per-trajectory cold-start latency later, add a warm pool (and optionally an HPA
  on `agent_sandbox_claim_creation_total`) — keep sandbox nodes warm so SWE-bench's
  shared base/env image layers stay cached.
- **Versions to confirm before running:** `KUBERAY_VERSION`, the Ray image tag in
  `raycluster-sample.yaml` (match SkyRL's Ray), and `mini-swe-agent` pinned to the
  v1 line (its v2 changed the `Environment.execute` signature).
- **Image locality:** mirror SWE-bench images into Artifact Registry in-region
  (and/or enable Image Streaming) so cold creates aren't pull-bound.
