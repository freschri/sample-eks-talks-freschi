#!/usr/bin/env bash
# Verify the workload is healthy after all fixes.
set -euo pipefail

NS="workload"
OK=true

echo "=== Pod Status ==="
kubectl get pods -n "$NS"

echo ""
echo "=== Checks ==="

# Secret exists
if kubectl get secret db-creds -n "$NS" &>/dev/null; then
  echo "✅ db-creds secret exists"
else
  echo "❌ db-creds secret missing"; OK=false
fi

# Redis service on correct port
PORT=$(kubectl get svc redis -n "$NS" -o jsonpath='{.spec.ports[0].port}')
if [ "$PORT" = "6379" ]; then
  echo "✅ redis service port is 6379"
else
  echo "❌ redis service port is $PORT (should be 6379)"; OK=false
fi

# sample-app memory limit
MEM=$(kubectl get deploy sample-app -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}')
if [ "$MEM" != "32Mi" ]; then
  echo "✅ sample-app memory limit is $MEM"
else
  echo "❌ sample-app memory limit is 32Mi (too low)"; OK=false
fi

# Pods running
READY=$(kubectl get deploy sample-app -n "$NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [ "${READY:-0}" -ge 1 ]; then
  echo "✅ sample-app has $READY ready replicas"
else
  echo "❌ sample-app has no ready replicas"; OK=false
fi

echo ""
if $OK; then
  echo "🎉 All checks passed — workload is healthy!"
else
  echo "⚠️  Some checks failed — keep fixing!"
fi
