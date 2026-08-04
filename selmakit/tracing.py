import logging

logger = logging.getLogger(__name__)

# Phoenix serves its UI *and* its OTLP/HTTP collector on 6006; the separate
# gRPC collector on 4317 is unused since the exporter speaks HTTP.
DEFAULT_ENDPOINT = "http://localhost:6006/v1/traces"


def setup(
    project_name: str = "selmakit",
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    capture_http: bool = True,
) -> None:
    """Activate OpenTelemetry tracing for the agent, exported over OTLP/HTTP.

    Call once at startup before the gateway starts accepting requests.
    Without this call the tracer is a no-op and the program runs unchanged.

    Built on the Logfire SDK, which ships with ``pydantic-ai`` — nothing extra
    to install. Logfire is used purely as an OpenTelemetry client here:
    ``send_to_logfire=False`` means **no data leaves the machine and no Logfire
    account or token is involved**. Spans go to ``endpoint`` only, by default
    the Phoenix collector (run it standalone, e.g. its Docker image, so it does
    not share this venv). Any OTLP/HTTP collector works as a drop-in; to use
    Logfire's hosted backend instead, configure it yourself rather than calling
    this function.

    ``capture_http`` additionally records the raw request/response bodies of
    the HTTP calls to the model provider — what actually went over the wire, as
    opposed to pydantic-ai's own view of the run. Headers are deliberately
    *not* captured: they carry the provider API keys.

    If the dependencies are missing, tracing is skipped and a warning is logged
    — the gateway continues without tracing.
    """
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    try:
        import logfire
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning("OpenTelemetry tracing disabled — missing dependency (%s)", e)
        return

    logfire.configure(
        service_name=project_name,
        # Phoenix groups spans by this resource attribute, not by service.name;
        # without it everything lands in its "default" project.
        resource_attributes={"openinference.project.name": project_name},
        send_to_logfire=False,
        console=False,
        # selmakit never uses Logfire's f-string magic, so skip the source
        # introspection — it warns noisily whenever source is unavailable
        # (interactive shells, exec(), .pyc without .py).
        inspect_arguments=False,
        additional_span_processors=[
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        ],
    )
    logfire.instrument_pydantic_ai(include_content=True)
    if capture_http:
        # Bodies only — capture_headers/capture_all would sweep up the
        # Authorization header of every hosted-provider call.
        logfire.instrument_httpx(capture_request_body=True, capture_response_body=True)

    logger.info(
        "OpenTelemetry tracing enabled → %s (project=%s, http_bodies=%s)",
        endpoint, project_name, capture_http,
    )
