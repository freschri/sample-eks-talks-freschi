"""SRE Agent — FastAPI server with streaming SSE and web UI."""
import os
import json
import logging
import asyncio
import concurrent.futures

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

from strands import Agent
from strands.models.openai import OpenAIModel
from tools import query_prometheus, get_pod_status, get_pod_logs, get_events, describe_resource

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

ALL_TOOLS = [query_prometheus, get_pod_status, get_pod_logs, get_events, describe_resource]

SYSTEM_PROMPT = (
    "You are an expert SRE agent. You diagnose Kubernetes application issues "
    "by systematically using your tools. IMPORTANT: call only ONE tool at a time, "
    "then analyze the result before deciding the next step. "
    "Start with get_pod_status, then get_events, then dig deeper with get_pod_logs "
    "and query_prometheus. Be specific about root causes and suggest exact fix commands."
)

# --- FastAPI ---
app = FastAPI(title="SRE Agent", description="AI-powered Kubernetes diagnostics")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


class DiagnoseRequest(BaseModel):
    prompt: str


class DiagnoseResponse(BaseModel):
    response: str


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_agent(prompt: str):
    """Run agent and yield SSE events by polling agent.messages."""
    agent = Agent(
        model=model,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
    )

    loop = asyncio.get_event_loop()
    error_holder = {}

    def run_agent():
        try:
            return agent(prompt)
        except Exception as e:
            error_holder["error"] = str(e)
            logger.exception("Agent error")
            return None

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(run_agent)
        last_len = 0

        while not future.done():
            await asyncio.sleep(0.5)
            msgs = agent.messages
            if len(msgs) > last_len:
                for msg in msgs[last_len:]:
                    for ev in _extract_events(msg):
                        yield ev
                last_len = len(msgs)

        # Collect any remaining messages
        future.result()
        msgs = agent.messages
        for msg in msgs[last_len:]:
            for ev in _extract_events(msg):
                yield ev

    if "error" in error_holder:
        yield _sse("error", error_holder["error"])

    provider.force_flush()
    yield _sse("done", "")


def _extract_events(msg):
    """Extract SSE events from a single Strands message."""
    role = msg.get("role", "")
    if role == "assistant":
        for block in msg.get("content", []):
            if "toolUse" in block:
                yield _sse("tool_call", f"🔧 Calling {block['toolUse']['name']}...")
            elif "text" in block:
                text = block["text"].strip()
                if text:
                    yield _sse("text", text)
    elif role == "tool":
        for block in msg.get("content", []):
            if "toolResult" in block:
                tr = block["toolResult"]
                status = "✅" if tr.get("status") == "success" else "❌"
                content = ""
                for c in tr.get("content", []):
                    if "text" in c:
                        content = c["text"][:300]
                        break
                yield _sse("tool_result", f"{status} {content}")


@app.post("/diagnose/stream")
async def diagnose_stream(req: DiagnoseRequest):
    return StreamingResponse(
        _stream_agent(req.prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/diagnose", response_model=DiagnoseResponse)
def diagnose(req: DiagnoseRequest):
    agent = Agent(model=model, tools=ALL_TOOLS, system_prompt=SYSTEM_PROMPT)
    result = agent(req.prompt)
    provider.force_flush()
    return DiagnoseResponse(response=str(result))


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/static/index.html") as f:
        return f.read()


@app.get("/health")
def health():
    return {"status": "ok"}
