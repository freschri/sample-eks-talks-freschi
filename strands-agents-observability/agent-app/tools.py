"""SRE diagnostic tools for the agent — thin wrappers around kubectl and Prometheus."""
import json
import subprocess
import urllib.request
import os
from strands import tool

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus-server.observability:80")


def _kubectl(*args: str) -> str:
    """Run a kubectl command and return stdout."""
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout + result.stderr


@tool
def query_prometheus(query: str) -> str:
    """Run a PromQL instant query against Prometheus.

    Args:
        query: A PromQL query string, e.g. 'kube_pod_container_status_restarts_total{namespace="workload"}'

    Returns:
        JSON string with the query results.
    """
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    results = data.get("data", {}).get("result", [])
    return json.dumps(results[:20], indent=2)


@tool
def get_pod_status(namespace: str) -> str:
    """Get the status of all pods in a namespace, including restarts and conditions.

    Args:
        namespace: The Kubernetes namespace to query.

    Returns:
        Pod listing with NAME, READY, STATUS, RESTARTS, and AGE.
    """
    return _kubectl("get", "pods", "-n", namespace, "-o", "wide")


@tool
def get_pod_logs(pod_name: str, namespace: str, tail_lines: int = 50) -> str:
    """Get the most recent logs from a pod. Uses --previous if the pod is in CrashLoopBackOff.

    Args:
        pod_name: Name of the pod (or deployment/ prefix for convenience).
        namespace: The Kubernetes namespace.
        tail_lines: Number of log lines to retrieve (default 50).

    Returns:
        The pod's log output.
    """
    # Try current logs first, fall back to previous
    out = _kubectl("logs", pod_name, "-n", namespace, f"--tail={tail_lines}")
    if not out.strip() or "is waiting to start" in out:
        out = _kubectl("logs", pod_name, "-n", namespace, f"--tail={tail_lines}", "--previous")
    return out


@tool
def get_events(namespace: str) -> str:
    """Get recent Kubernetes events in a namespace, sorted by time. Useful for spotting OOMKilled, FailedScheduling, CrashLoopBackOff, etc.

    Args:
        namespace: The Kubernetes namespace.

    Returns:
        Event listing with LAST SEEN, TYPE, REASON, OBJECT, and MESSAGE.
    """
    return _kubectl("get", "events", "-n", namespace, "--sort-by=.lastTimestamp")


@tool
def describe_resource(resource: str, name: str, namespace: str) -> str:
    """Run kubectl describe on a Kubernetes resource. Useful for checking resource limits, probe config, service endpoints, etc.

    Args:
        resource: Resource type, e.g. 'pod', 'deployment', 'service', 'secret'.
        name: Name of the resource.
        namespace: The Kubernetes namespace.

    Returns:
        Full kubectl describe output.
    """
    return _kubectl("describe", resource, name, "-n", namespace)


import urllib.parse  # noqa: E402 — needed by query_prometheus
