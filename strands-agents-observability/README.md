# Agent Observability Demo — SRE Agent on EKS with FluxCD

An AI-powered SRE agent that diagnoses Kubernetes failures, deployed via FluxCD GitOps on EKS Auto Mode with self-hosted LLM inference (vLLM) and fully OSS observability.

Code is provided as reference for demo purposes. In a production environment, restrict privileges according to the principle of least privilege.

## Prerequisites

- AWS account
- HuggingFace token (for Llama 3.1 8B access)
- **Fork this repository** (Flux will commit its files to it)

## Deployment

1. **Set environment variables**:

```bash
export CLUSTER_NAME=agent-obs-demo
export AWS_DEFAULT_REGION=eu-west-2
export HF_TOKEN=<your-huggingface-token>
export GITHUB_TOKEN=<your-github-token>
export GITHUB_USER=<your-github-user>
export GITHUB_REPO=<your-forked-repo>
```

2. **Create the EKS cluster**:

```bash
cat << EOF | eksctl create cluster -f -
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: ${CLUSTER_NAME}
  region: ${AWS_DEFAULT_REGION}

autoModeConfig:
  enabled: true
EOF
```

3. **Build and push the SRE agent image**:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com/${CLUSTER_NAME}/agent-app"

aws ecr create-repository --repository-name "${CLUSTER_NAME}/agent-app" --region "${AWS_DEFAULT_REGION}" 2>/dev/null || true
aws ecr get-login-password --region "${AWS_DEFAULT_REGION}" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com"

docker build --platform linux/amd64 -t "${ECR_REPO}:latest" strands-agents-observability/agent-app/
docker push "${ECR_REPO}:latest"
```

4. **Create secrets and Flux ConfigMap**:

```bash
kubectl create namespace vllm
kubectl create secret generic hf-token \
  --from-literal=token="${HF_TOKEN}" \
  --namespace vllm

kubectl create namespace flux-system

cat << EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-config
  namespace: flux-system
data:
  ECR_REPO: "${ECR_REPO}"
EOF
```

5. **Bootstrap Flux**:

```bash
brew install fluxcd/tap/flux

flux bootstrap github \
  --owner=${GITHUB_USER} \
  --repository=${GITHUB_REPO} \
  --branch=main \
  --personal \
  --path=strands-agents-observability/cluster
```

6. **Monitor deployment**:

```bash
flux get kustomizations --watch
```

7. **Port-forward**:

```bash
kubectl port-forward svc/sre-agent 8000:8000 -n agent-app &
kubectl port-forward svc/grafana 3000:80 -n observability &
kubectl port-forward svc/jaeger 16686:16686 -n observability &
```

## Demo

1. Open the SRE Agent UI: http://localhost:8000
2. Open Grafana: http://localhost:3000 (admin / agent-obs-demo)
3. Open Jaeger: http://localhost:16686

Arrange all three browser windows side by side.

4. **Inject chaos**:
```bash
./strands-agents-observability/chaos/inject.sh
```

5. **Check Grafana** — open the "Agent Observability" dashboard. You should see:
   - Redis connected clients dropping
   - Nginx error rate increasing
   - vLLM KV cache usage and inference latency (will light up when the agent runs)

6. **Ask the agent** using the preset prompts in the web UI

7. **Check Jaeger** — select service `sre-agent` and click "Find Traces". Click on a trace to see:
   - The full span waterfall: `invoke_agent` → `execute_event_loop_cycle` → `chat` (LLM call) → `execute_tool` (kubectl/PromQL)
   - How long each LLM call takes vs. each tool call
   - The tool inputs and outputs in the span logs (click a span → "Logs" tab)

8. **Fix layer 1** (missing secret), then re-ask the agent:
```bash
kubectl create secret generic db-creds -n workload --from-literal=DB_PASSWORD=demo123
```

9. **Fix layer 2** (OOMKill), then re-ask the agent:
```bash
kubectl set resources deploy/sample-app -n workload --requests=memory=128Mi --limits=memory=256Mi
```

10. **Fix layer 3** (wrong redis port):
```bash
kubectl patch svc redis -n workload --type='json' \
  -p='[{"op":"replace","path":"/spec/ports/0/port","value":6379},{"op":"replace","path":"/spec/ports/0/targetPort","value":6379}]'
```

After each fix, re-ask the agent and check Jaeger — each diagnosis creates a new trace showing the agent peeling back the next layer.

### Autonomous Mode (Auto-Fix)

Instead of manually diagnosing and fixing, you can let the agent detect and fix issues autonomously:

1. Click **🤖 Start Auto-Fix** in the web UI
2. The agent scans the workload namespace every 60 seconds
3. When it finds unhealthy pods, it calls a **fixer agent** (agent-as-tool pattern) that diagnoses the root cause and applies the fix
4. Each cycle creates a fresh agent with clean context — no token bloat across cycles
5. The loop stops automatically when all pods are healthy, or click **Stop**

The auto-fix uses two agents:
- **Detector Agent** — lightweight scan with `get_pod_status` + `get_events`. If it finds a problem, it calls `fix_issue`
- **Fixer Agent** (wrapped as a `@tool`) — deeper diagnosis with logs/prometheus, then applies the fix using `kubectl_create_secret`, `kubectl_set_resources`, or `kubectl_patch_service`

> **Note on FluxCD:** `inject.sh` suspends the Flux workload kustomization before injecting faults, so neither the chaos nor the agent's fixes get reverted. `verify.sh` resumes it when all checks pass.

> **Production consideration:** In production, the agent should not apply fixes directly. Instead, it would commit the fix to the Git repo (or open a PR) and let FluxCD reconcile — keeping the GitOps single source of truth. A human-in-the-loop approval step on the PR adds a safety gate before changes reach the cluster.

9. **Verify**:
```bash
./strands-agents-observability/chaos/verify.sh
```

## Architecture

![Architecture](./images/architecture.png)

## FluxCD Dependency Chain

```
infra (Karpenter GPU NodePool)
  ├── observability (Jaeger, Prometheus, OTel Collector, Grafana)
  ├── vllm (Llama 3.1 8B on GPU)
  └── workload (nginx + sample-app + redis)
        └── agent-app (SRE agent — depends on all above)
```

## Repo Structure

```
├── cluster/
│   └── development.yaml              # Flux Kustomizations + dependency chain
├── infra/
│   └── karpenter-gpu-nodepool.yaml   # GPU nodes for vLLM
├── observability/
│   ├── helm-releases.yaml            # Jaeger, Prometheus, OTel, Grafana
│   └── dashboard-configmap.yaml      # Grafana dashboard JSON
├── vllm/
│   └── deployment.yaml               # Llama 3.1 8B on GPU
├── workload/
│   └── manifests.yaml                # nginx + redis + sample-app (with exporters)
├── agent-app/
│   ├── manifests.yaml                # K8s Deployment + Service + RBAC
│   ├── app.py                        # FastAPI SRE agent (streaming SSE)
│   ├── tools.py                      # 5 diagnostic + 3 fix tools
│   ├── static/index.html             # Chat web UI
│   ├── Dockerfile
│   └── requirements.txt
└── chaos/
    ├── inject.sh                     # Inject 3 layered faults
    └── verify.sh                     # Verify workload health
```

## Cleanup

```bash
flux uninstall
eksctl delete cluster --name ${CLUSTER_NAME} --region ${AWS_DEFAULT_REGION}
aws ecr delete-repository --repository-name "${CLUSTER_NAME}/agent-app" --region "${AWS_DEFAULT_REGION}" --force
```

## Cost

~$1.50/hr while running (mostly the GPU node). Clean up when done.
