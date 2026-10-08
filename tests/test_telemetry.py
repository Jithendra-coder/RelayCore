from __future__ import annotations

from opentelemetry import baggage
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_current_span

from app.telemetry import (configure_tracing, current_traceparent, mark_span_failed, trace_context,
                           workflow_step_span)


def test_trace_context_propagates_traceparent_without_baggage():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("relaycore.test")
    try:
        with tracer.start_as_current_span("api.request") as parent:
            token = attach(baggage.set_baggage("private", "do-not-propagate"))
            try:
                traceparent = current_traceparent()
            finally:
                detach(token)
            parent_context = parent.get_span_context()

        task = {
            "traceparent": traceparent,
            "request_id": "request-123",
            "run_id": "run-456",
            "id": "task-789",
            "attempts": 2,
            "step_index": 1,
            "definition": {"steps": [{"payload": {"secret": "private"}}]},
        }
        with workflow_step_span(tracer, task, "worker-1") as worker_span:
            assert worker_span.get_span_context().trace_id == parent_context.trace_id
            mark_span_failed(worker_span, ValueError("private provider response"))

        propagated = get_current_span(trace_context(traceparent)).get_span_context()
        assert propagated.is_remote
        assert propagated.trace_id == parent_context.trace_id
        assert propagated.span_id == parent_context.span_id
        assert "do-not-propagate" not in traceparent
        assert not get_current_span(trace_context("invalid-traceparent")).get_span_context().is_valid
        finished = {span.name: span for span in exporter.get_finished_spans()}
        worker = finished["relaycore.workflow.step"]
        assert worker.parent.span_id == parent_context.span_id
        assert worker.attributes["relaycore.request_id"] == "request-123"
        assert worker.attributes["error.type"] == "ValueError"
        assert "private" not in repr(worker.attributes) + repr(worker.events)
    finally:
        provider.shutdown()


def test_tracing_stays_disabled_without_an_otlp_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert configure_tracing("relaycore-test") is None


def test_tracing_uses_the_otlp_endpoint_and_service_name(monkeypatch):
    import app.telemetry as telemetry

    providers = []
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", providers.append)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    provider = configure_tracing("relaycore-worker")
    assert provider is providers[0]
    assert provider.resource.attributes["service.name"] == "relaycore-worker"
    provider.shutdown()
