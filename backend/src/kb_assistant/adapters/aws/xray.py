"""Sends the agent loop's spans to X-Ray, inside the chat Lambda's own trace.

Strands records an OpenTelemetry span for the agent, each loop cycle, each model call
and each tool call. Lambda runs an X-Ray daemon that takes subsegments over UDP, so each
finished span is sent as a subsegment under the function's segment. The Lambda Web
Adapter forwards that segment's trace header with every request, and only requests that
X-Ray sampled are recorded.

Spans carry prompts, answers, tool input and tool results as events, and with some
Strands settings as attributes. None of that is sent: a subsegment holds the span's
name, timing, error status and an allowlist of attributes (model, token counts, model
latency, tool name and status).
"""

import json
import re
import socket
from collections.abc import Sequence
from typing import Any

from opentelemetry import context, trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, StatusCode, TraceFlags
from starlette.types import ASGIApp, Receive, Scope, Send

DAEMON_HEADER = b'{"format": "json", "version": 1}\n'
ATTRIBUTES = {
    "gen_ai.operation.name",
    "gen_ai.agent.name",
    "gen_ai.request.model",
    "gen_ai.server.time_to_first_token",
    "gen_ai.server.request.duration",
    "gen_ai.tool.name",
    "gen_ai.tool.status",
    "event_loop.cycle_id",
}
ATTRIBUTE_PREFIXES = ("gen_ai.usage.",)
# X-Ray names allow letters, digits, whitespace and these symbols, up to 200 characters.
NAME_REJECTED = re.compile(r"[^\w\s.:/%&#=+\\@-]")
ROOT = re.compile(r"1-([0-9a-f]{8})-([0-9a-f]{24})")
SEGMENT_ID = re.compile(r"[0-9a-f]{16}")

_exporter: "XRayExporter | None" = None


def configure_tracing(address: str) -> "XRayExporter":
    """Records sampled requests' spans for the daemon at `address`, once per process."""
    global _exporter
    if _exporter is None:
        _exporter = XRayExporter(address)
        # A span is recorded only under a sampled Lambda segment, never as a new trace.
        provider = TracerProvider(sampler=ParentBased(ALWAYS_OFF))
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
    return _exporter


class TraceContext:
    """Makes the Lambda function's segment the parent of the spans a request records."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        parent = lambda_segment(scope) if scope["type"] == "http" else None
        if parent is None:
            await self.app(scope, receive, send)
            return
        token = context.attach(trace.set_span_in_context(NonRecordingSpan(parent)))
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)


def lambda_segment(scope: Scope) -> SpanContext | None:
    """The function segment named by the Lambda context header the adapter sets."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-amzn-lambda-context":
            try:
                header = json.loads(value)["xray_trace_id"]
            except Exception:
                return None
            return parse_trace_header(header) if isinstance(header, str) else None
    return None


def parse_trace_header(header: str) -> SpanContext | None:
    """Reads `Root=1-...;Parent=...;Sampled=1` as the span context of its parent."""
    fields = {}
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        fields[key] = value
    root = ROOT.fullmatch(fields.get("Root", ""))
    parent = fields.get("Parent", "")
    if root is None or not SEGMENT_ID.fullmatch(parent):
        return None
    sampled = fields.get("Sampled") == "1"
    return SpanContext(
        trace_id=int(root[1] + root[2], 16),
        span_id=int(parent, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
    )


class XRayExporter(SpanExporter):
    """Sends each finished span to the X-Ray daemon as a subsegment."""

    def __init__(self, address: str) -> None:
        self.destination = daemon_destination(address)
        self._socket: socket.socket | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            document = subsegment(span)
            if document is None:
                continue
            try:
                if self._socket is None:
                    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.sendto(
                    DAEMON_HEADER + json.dumps(document, separators=(",", ":")).encode(),
                    self.destination,
                )
            except OSError:
                # Tracing never fails a turn; a lost subsegment only leaves a gap in it.
                return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def daemon_destination(address: str) -> tuple[str, int]:
    """The UDP host and port in `host:port`, or in the `tcp:... udp:...` form."""
    parts = address.split()
    chosen = next((part[4:] for part in parts if part.startswith("udp:")), parts[0])
    host, port = chosen.rsplit(":", 1)
    return host, int(port)


def subsegment(span: ReadableSpan) -> dict[str, Any] | None:
    if span.parent is None or span.start_time is None or span.end_time is None:
        return None
    trace_id = f"{span.context.trace_id:032x}"
    document: dict[str, Any] = {
        "name": NAME_REJECTED.sub("_", span.name)[:200] or "span",
        "id": f"{span.context.span_id:016x}",
        "trace_id": f"1-{trace_id[:8]}-{trace_id[8:]}",
        "parent_id": f"{span.parent.span_id:016x}",
        "type": "subsegment",
        "start_time": span.start_time / 1e9,
        "end_time": span.end_time / 1e9,
    }
    attributes = {
        name: value
        for name, value in (span.attributes or {}).items()
        if (name in ATTRIBUTES or name.startswith(ATTRIBUTE_PREFIXES))
        and isinstance(value, (str, bool, int, float))
    }
    if attributes:
        document["metadata"] = {"strands": attributes}
    if span.status.status_code is StatusCode.ERROR:
        document["fault"] = True
    return document
