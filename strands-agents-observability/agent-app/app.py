"""SRE Agent — FastAPI server with autonomous detect-fix loop."""
import os
import json
import logging
import asyncio

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

from strands import Agent, tool
from tools import (
    query_prometheus, get_pod_status, get_pod_logs, get_events, describe_resource,
    kubectl_create_secret, kubectl_set_resources, kubectl_patch_service,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- OTel setup ---
OTEL_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector-opentelemetry-collector.observability:4317")
resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "sre-agent")})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)))
trace.set_tracer_provider(provider)

# --- Model setup (Bedrock default, vLLM optional) ---
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "bedrock")

if MODEL_PROVIDER == "vllm":
    from strands.models.openai import OpenAIModel
    model = OpenAIModel(
        client_args={
            "base_url": os.environ.get("VLLM_BASE_URL", "http://vllm-svc.vllm:8000/v1"),
            "api_key": "not-needed",
        },
        model_id=os.environ.get("VLLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct"),
        params={"parallel_tool_calls": False},
    )
else:
    from strands.models.bedrock import BedrockModel
    model = BedrockModel(
        model_id=os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-4-6"),
        region_name=os.environ.get("AWS_REGION", "eu-west-2"),
    )

logger.info(f"Using model provider: {MODEL_PROVIDER}")

# --- Tools ---
DIAGNOSE_TOOLS = [query_prometheus, get_pod_status, get_pod_logs, get_events, describe_resource]
FIX_TOOLS = DIAGNOSE_TOOLS + [kubectl_create_secret, kubectl_set_resources, kubectl_patch_service]

# --- Prompts ---
DETECTOR_PROMPT = """\
You are an SRE agent monitoring a Kubernetes cluster. Your job is to detect unhealthy pods in the workload namespace.

Workflow:
1. Call get_pod_status for namespace "workload"
2. Examine the STATUS column of each pod
3. If all pods show Running with full readiness (e.g. 1/1 or 2/2), respond with exactly: ALL_HEALTHY
4. If any pod has a failure status (CrashLoopBackOff, CreateContainerConfigError, Error, OOMKilled, ImagePullBackOff), call fix_issue with the pod name and its status
5. Ignore transient states like ContainerCreating, PodInitializing, Pending, Terminating — these are normal

Important: base your decision only on the current pod status, not on events or past state."""

FIXER_PROMPT = """\
You are an SRE agent that diagnoses and fixes Kubernetes issues in the workload namespace.

You have diagnostic tools (get_events, get_pod_logs, describe_resource, query_prometheus) and fix tools (kubectl_create_secret, kubectl_set_resources, kubectl_patch_service).

Workflow:
1. Start by calling get_events for namespace "workload" to understand the root cause
2. If needed, use get_pod_logs or describe_resource for more detail
3. Apply the appropriate fix — match the error to the right tool:
   - "secret not found" → kubectl_create_secret
   - OOMKilled → kubectl_set_resources (increase memory)
   - Connection refused on wrong port → kubectl_patch_service
4. After fixing, call get_pod_status for namespace "workload" to verify

Use real resource names from tool output. Never guess or use placeholders."""

# --- Shared event log ---
_events: list[str] = []
_autofix_running = False
_autofix_task = None


def _log(msg: str):
    _events.append(msg)
    logger.info(msg)


def _extract_actions(agent) -> list[str]:
    """Extract tool calls and reasoning from agent messages."""
    actions = []
    for msg in agent.messages:
        role = msg.get("role", "")
        if role == "assistant":
            for block in msg.get("content", []):
                if "toolUse" in block:
                    actions.append(f"  🔧 {block['toolUse']['name']}({_summarize_input(block['toolUse'].get('input', {}))})")
                elif "text" in block:
                    text = block["text"].strip()
                    if text:
                        actions.append(f"  💭 {text[:300]}")
        elif role == "tool":
            for block in msg.get("content", []):
                if "toolResult" in block:
                    tr = block["toolResult"]
                    icon = "✅" if tr.get("status") == "success" else "❌"
                    content = ""
                    for c in tr.get("content", []):
                        if "text" in c:
                            content = c["text"][:200]
                            break
                    actions.append(f"  {icon} {content}")
    return actions


def _summarize_input(inp: dict) -> str:
    parts = []
    for k, v in inp.items():
        s = str(v)
        parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)


# --- Fixer agent-as-tool ---
@tool
def fix_issue(problem_description: str) -> str:
    """Diagnose the root cause of a Kubernetes issue and apply a fix.

    Args:
        problem_description: Description of the problem detected, including pod name and status.

    Returns:
        Summary of what was fixed.
    """
    _log(f"🔧 Fixer agent invoked: {problem_description[:200]}")
    fixer = Agent(model=model, tools=FIX_TOOLS, system_prompt=FIXER_PROMPT)
    result = fixer(problem_description)
    for action in _extract_actions(fixer):
        _log(action)
    provider.force_flush()
    return str(result)


# --- Autonomous loop ---
async def _autofix_loop(interval: int):
    global _autofix_running
    _autofix_running = True
    _events.clear()
    _log(f"🤖 Agent started ({MODEL_PROVIDER}). Scanning every {interval}s...")

    cycle = 0
    while _autofix_running:
        cycle += 1
        _log(f"🔄 Cycle {cycle}: scanning workload namespace...")

        detector = Agent(model=model, tools=[get_pod_status, fix_issue], system_prompt=DETECTOR_PROMPT)

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: detector("Scan the workload namespace for unhealthy pods.")
            )
            for action in _extract_actions(detector):
                _log(action)

            text = str(result)
            provider.force_flush()

            if "ALL_HEALTHY" in text.upper():
                _log(f"✅ Cycle {cycle}: All pods healthy.")
            else:
                _log(f"📋 Cycle {cycle}: done.")
        except Exception as e:
            _log(f"❌ Cycle {cycle}: Error — {e}")
            logger.exception("Autofix error")

        for _ in range(interval):
            if not _autofix_running:
                break
            await asyncio.sleep(1)

    _autofix_running = False
    _log("🛑 Agent stopped.")


# --- FastAPI ---
app = FastAPI(title="SRE Agent", description="AI-powered Kubernetes auto-fix")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.post("/autofix/start")
async def autofix_start(interval: int = Query(default=15, ge=5, le=300)):
    global _autofix_running, _autofix_task
    if _autofix_running:
        return {"status": "already_running"}
    _autofix_task = asyncio.create_task(_autofix_loop(interval))
    return {"status": "started", "interval": interval}


@app.post("/autofix/stop")
async def autofix_stop():
    global _autofix_running
    _autofix_running = False
    return {"status": "stopping"}


@app.get("/autofix/events")
async def autofix_events():
    return {"running": _autofix_running, "events": _events}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/static/index.html") as f:
        return f.read()


@app.get("/health")
def health():
    return {"status": "ok"}
