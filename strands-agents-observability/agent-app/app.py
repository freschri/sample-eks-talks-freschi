"""SRE Agent — FastAPI server with autonomous detect-fix loop."""
import os
import json
import logging
import asyncio

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

from strands import Agent, tool
from strands.models.openai import OpenAIModel
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

# --- vLLM model ---
model = OpenAIModel(
    client_args={
        "base_url": os.environ.get("VLLM_BASE_URL", "http://vllm-svc.vllm:8000/v1"),
        "api_key": "not-needed",
    },
    model_id=os.environ.get("VLLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct"),
    params={"parallel_tool_calls": False},
)

# --- Tools ---
DIAGNOSE_TOOLS = [query_prometheus, get_pod_status, get_pod_logs, get_events, describe_resource]
FIX_TOOLS = DIAGNOSE_TOOLS + [kubectl_create_secret, kubectl_set_resources, kubectl_patch_service]

# --- Shared event log ---
_events: list[str] = []


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
        problem_description: Description of the problem detected.

    Returns:
        Summary of what was fixed.
    """
    _log(f"🔧 Fixer agent invoked: {problem_description[:200]}")
    fixer = Agent(
        model=model,
        tools=FIX_TOOLS,
        system_prompt=(
            "You are an SRE agent that fixes Kubernetes issues. "
            "You have tools to create secrets, set resource limits, and patch services. "
            "IMPORTANT: call only ONE tool at a time. "
            "NEVER use placeholder names like 'pod-name' — always use real names from tool output. "
            "First call get_events for namespace 'workload' to understand the root cause. "
            "Then apply the fix using the appropriate tool. Only fix in the 'workload' namespace. "
            "After fixing, call get_pod_status for namespace 'workload' to verify."
        ),
    )
    result = fixer(problem_description)
    for action in _extract_actions(fixer):
        _log(action)
    provider.force_flush()
    return str(result)


# --- Autonomous loop ---
_autofix_running = False


async def _autofix_loop():
    global _autofix_running
    _autofix_running = True
    _events.clear()
    _log("🤖 Agent started. Watching workload namespace...")

    cycle = 0
    while _autofix_running:
        cycle += 1
        _log(f"🔄 Cycle {cycle}: scanning workload namespace...")

        detector = Agent(
            model=model,
            tools=[get_pod_status, get_events, fix_issue],
            system_prompt=(
                "You are an SRE detector agent. Scan the workload namespace for unhealthy pods. "
                "IMPORTANT: call only ONE tool at a time. "
                "Start with get_pod_status for namespace 'workload'. "
                "Only consider a pod unhealthy if its STATUS is one of: CrashLoopBackOff, "
                "CreateContainerConfigError, Error, OOMKilled, ImagePullBackOff. "
                "Ignore pods in ContainerCreating, PodInitializing, Pending, or Terminating — these are transient. "
                "If you find an unhealthy pod, call fix_issue with the pod name and error from the STATUS column. "
                "If all pods show Running with READY n/n, respond with exactly 'ALL_HEALTHY'."
            ),
        )

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: detector("Scan the workload namespace for issues and fix any you find.")
            )
            for action in _extract_actions(detector):
                _log(action)

            text = str(result)
            provider.force_flush()

            if "ALL_HEALTHY" in text.upper():
                _log(f"✅ Cycle {cycle}: All pods healthy. Watching...")
            else:
                _log(f"📋 Cycle {cycle}: done. Next scan in 60s...")
        except Exception as e:
            _log(f"❌ Cycle {cycle}: Error — {e}")
            logger.exception("Autofix error")

        for _ in range(60):
            if not _autofix_running:
                break
            await asyncio.sleep(1)

    _autofix_running = False
    _log("🛑 Agent stopped.")


# --- FastAPI ---
app = FastAPI(title="SRE Agent", description="AI-powered Kubernetes auto-fix")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.on_event("startup")
async def startup():
    asyncio.create_task(_autofix_loop())


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
