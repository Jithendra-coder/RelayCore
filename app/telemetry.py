from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


def configure_tracing(service_name: str) -> TracerProvider | None:
    if not any(os.environ.get(name, "").strip() for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    )):
        return None
    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
    }))
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(timeout=2), schedule_delay_millis=5000, export_timeout_millis=2000,
    ))
    trace.set_tracer_provider(provider)
    return provider


def trace_context(traceparent: str | None) -> Context:
    return _propagator.extract({"traceparent": traceparent or ""})


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


def http_request_span(tracer: trace.Tracer, headers: Mapping[str, str]) -> AbstractContextManager[trace.Span]:
    return tracer.start_as_current_span(
        "http.request",
        context=trace_context(headers.get("traceparent")),
        kind=trace.SpanKind.SERVER,
        record_exception=False,
        set_status_on_exception=False,
    )


def workflow_step_span(
    tracer: trace.Tracer, task: dict[str, Any], worker_id: str,
) -> AbstractContextManager[trace.Span]:
    return tracer.start_as_current_span(
        "relaycore.workflow.step",
        context=trace_context(task.get("traceparent")),
        attributes={
            "relaycore.request_id": task["request_id"],
            "relaycore.workflow.run_id": task["run_id"],
            "relaycore.workflow.task_id": task["id"],
            "relaycore.worker.id": worker_id,
            "relaycore.attempt": task["attempts"],
            "relaycore.step.index": task["step_index"],
        },
        record_exception=False,
        set_status_on_exception=False,
    )


def mark_span_failed(span: trace.Span, error: Exception) -> None:
    span.set_attribute("error.type", type(error).__name__)
    span.set_status(trace.Status(trace.StatusCode.ERROR, "workflow step failed"))


def mark_http_span_failed(span: trace.Span) -> None:
    span.set_status(trace.Status(trace.StatusCode.ERROR, "HTTP server error"))
