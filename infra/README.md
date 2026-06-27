# GKE Infra for skyrl-sandbox

Provisions a standard GKE cluster wired for SkyRL-style agentic RL: KubeRay drives
the training/inference cluster on GPUs, and `agent-sandbox` runs untrusted
SWE-bench command execution in gVisor-isolated pods.

## Topology

```
GKE cluster (one control plane, VPC-native → flat pod networking)
├─ default-pool          e2-standard-4  ×1     system pods + kuberay-operator + agent-sandbox controller
├─ kuberay-gpu-pool      g2/a3 + GPUs   0..N    Ray head/workers   (taint: workload=ray-gpu, + GKE GPU taint)
└─ sandbox-gvisor-pool   e2-standard-8  1..N    Sandbox pods       (gVisor; taint: sandbox.gke.io/runtime=gvisor)
```

The two operators are lightweight and stay on `default-pool` (they have no
tolerations, so the tainted pools reject them). Heavy workloads are pinned to
their pools by nodeSelector + tolerations.

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
