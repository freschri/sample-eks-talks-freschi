#!/usr/bin/env bash
# Inject 3 layered faults into the workload namespace.
# Layer 1 (surface): CrashLoopBackOff — delete the db-creds secret
# Layer 2 (hidden):  OOMKill — set memory limit to 32Mi
# Layer 3 (hidden):  Connection refused — patch redis service port to 6380
set -euo pipefail

NS="workload"

echo ">>> Suspending Flux workload kustomization (prevents auto-revert)..."
flux suspend kustomization workload

echo ">>> Layer 3: Patching redis service port to 6380 (wrong port)..."
kubectl patch svc redis -n "$NS" --type='json' -p='[{"op":"replace","path":"/spec/ports/0/port","value":6380},{"op":"replace","path":"/spec/ports/0/targetPort","value":6380}]'

echo ">>> Layer 2: Setting sample-app memory limit to 32Mi (will OOMKill)..."
kubectl set resources deploy/sample-app -n "$NS" --requests=memory=16Mi --limits=memory=32Mi

echo ">>> Layer 1: Deleting db-creds secret (will CrashLoop)..."
kubectl delete secret db-creds -n "$NS" --ignore-not-found

echo ">>> Restarting sample-app to trigger failures..."
kubectl rollout restart deploy/sample-app -n "$NS"

echo ""
echo "All 3 faults injected. Flux workload kustomization suspended."
echo "Fix order: secret → memory → redis port"
