import logging

logger = logging.getLogger(__name__)


def setup(project_name: str = "selmakit", endpoint: str = "http://localhost:4317") -> None:
    """Activate OpenTelemetry tracing with pydantic-ai's built-in instrumentation.

    Call once at startup before the gateway starts accepting requests.
    Without this call the tracer is a no-op and the program runs unchanged.

    Spans are exported over OTLP/gRPC to ``endpoint`` (default the Phoenix
    collector at ``localhost:4317``). This sets up the OTel SDK directly
    instead of going through ``phoenix.otel.register`` — the ``arize-phoenix``
    Python package still pins ``pydantic-ai-slim<2`` and crashes on import
    under pydantic-ai 2.x. Run the Phoenix UI as a standalone process (e.g.
    its Docker image) so it does not share this venv; it just needs to listen
    on the OTLP endpoint.

    If the OTel SDK or exporter is missing, tracing is skipped and a warning is
    logged — the gateway continues without tracing.
    """
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pydantic_ai import Agent
        from pydantic_ai.agent import InstrumentationSettings
    except ImportError as e:
        logger.warning("OpenTelemetry tracing disabled — missing dependency (%s)", e)
        return

    provider = TracerProvider(resource=Resource.create({"service.name": project_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    Agent.instrument_all(
        InstrumentationSettings(tracer_provider=provider, include_content=True)
    )
    logger.info("OpenTelemetry tracing enabled → %s (project=%s)", endpoint, project_name)
