"""Observability: structured latency tracking + optional OpenTelemetry tracing/metrics.

Design goals
------------
* **Zero-cost when disabled** (``OTEL_ENABLED=false``, the default): falls
  back to structured ``structlog`` timing events only — no new dependency is
  required to get per-tool latency numbers in the logs.
* **Graceful degradation**: if ``OTEL_ENABLED=true`` but the
  ``opentelemetry-sdk``/``opentelemetry-exporter-otlp`` packages aren't
  installed, this logs one warning and continues in structlog-only mode
  rather than crashing the server.
* **Zero call-site changes**: :func:`instrument_public_methods` wraps every
  public (non-underscore) method of an object in place, so all 39 MCP tools
  get latency tracking / tracing automatically from one call in
  ``KnowledgeService.__init__`` — no per-tool decorator boilerplate, and no
  risk of drifting out of sync as new tools are added.
"""
from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_tracer: Any = None
_latency_histogram: Any = None
_retrieval_quality_histogram: Any = None
_embedding_batch_histogram: Any = None
_configured = False


def configure_observability(settings: Settings) -> None:
    """Idempotently set up OpenTelemetry tracing + metrics, if enabled/available."""
    global _tracer, _latency_histogram, _retrieval_quality_histogram
    global _embedding_batch_histogram, _configured
    if _configured:
        return
    _configured = True

    if not settings.otel_enabled:
        log.info("observability_disabled", hint="set OTEL_ENABLED=true to enable tracing/metrics")
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            ConsoleMetricExporter,
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        resource = Resource.create({"service.name": settings.otel_service_name})

        if settings.otel_exporter == "otlp" and settings.otel_exporter_endpoint:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            span_exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otel_exporter_endpoint))
        else:
            span_exporter = ConsoleSpanExporter()
            metric_reader = PeriodicExportingMetricReader(ConsoleMetricExporter())

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(settings.otel_service_name)

        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter(settings.otel_service_name)
        _latency_histogram = meter.create_histogram(
            "mcp_kb.tool.latency_ms", unit="ms",
            description="MCP tool call latency in milliseconds")
        _retrieval_quality_histogram = meter.create_histogram(
            "mcp_kb.retrieval.top_score", unit="1",
            description="Top fused/reranked retrieval score per query")
        _embedding_batch_histogram = meter.create_histogram(
            "mcp_kb.embedding.batch_latency_ms", unit="ms",
            description="Embedding batch latency in milliseconds")

        log.info("observability_configured", exporter=settings.otel_exporter,
                  service=settings.otel_service_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        log.warning("otel_sdk_not_installed_falling_back_to_structlog_only", error=str(exc),
                     hint="pip install opentelemetry-sdk opentelemetry-exporter-otlp")
    except Exception as exc:  # pragma: no cover - defensive, never crash the server on this
        log.warning("observability_setup_failed", error=str(exc))


def record_tool_latency(tool_name: str, elapsed_ms: float, *, error: bool = False) -> None:
    """Record a tool call's latency to the OTel histogram, if configured."""
    # log.info("tool_latency", tool=tool_name, elapsed_ms=round(elapsed_ms, 2), error=error)
    if _latency_histogram is not None:
        _latency_histogram.record(elapsed_ms, {"tool": tool_name, "error": str(error)})


def record_retrieval_quality(query: str, *, chunk_count: int, top_score: float) -> None:
    """Record a retrieval call's top score/chunk count to the OTel histogram, if configured."""
    log.debug("retrieval_quality", chunk_count=chunk_count, top_score=round(top_score, 4),
              query_preview=query[:80])
    if _retrieval_quality_histogram is not None:
        _retrieval_quality_histogram.record(top_score, {"chunk_count": str(chunk_count)})


def record_embedding_batch(*, batch_size: int, elapsed_ms: float) -> None:
    """Record an embedding batch's size/latency to the OTel histogram, if configured."""
    log.debug("embedding_batch", batch_size=batch_size, elapsed_ms=round(elapsed_ms, 2))
    if _embedding_batch_histogram is not None:
        _embedding_batch_histogram.record(elapsed_ms, {"batch_size": str(batch_size)})


def traced(name: str) -> Callable:
    """Decorator recording latency (+ an OTel span, if configured) for one call."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            error = False
            span_cm = _tracer.start_as_current_span(name) if _tracer is not None else None
            try:
                if span_cm is not None:
                    with span_cm:
                        return fn(*args, **kwargs)
                return fn(*args, **kwargs)
            except Exception:
                error = True
                raise
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                record_tool_latency(name, elapsed_ms, error=error)

        return wrapper

    return decorator


def instrument_public_methods(instance: Any, *, exclude: set[str] | None = None) -> None:
    """Wrap every public (non-underscore) method of ``instance`` with :func:`traced`.

    Mutates the instance in place (binds new instance attributes shadowing
    the class methods), so this must be called once per instance, typically
    at the end of ``__init__``. Idempotent per-instance via a guard attribute.
    """
    if getattr(instance, "_kb_instrumented", False):
        return
    exclude = exclude or set()
    cls = type(instance)
    for attr_name in dir(cls):
        if attr_name.startswith("_") or attr_name in exclude:
            continue
        attr = getattr(cls, attr_name, None)
        if not callable(attr):
            continue
        bound = getattr(instance, attr_name)
        if not callable(bound):
            continue
        traced_name = f"{cls.__name__}.{attr_name}"
        setattr(instance, attr_name, traced(traced_name)(bound))
    instance._kb_instrumented = True
