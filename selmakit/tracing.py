import logging

logger = logging.getLogger(__name__)

# Conventional OTLP/HTTP port. The exporter speaks HTTP, not gRPC, so this is
# the /v1/traces path on 4318 rather than the gRPC collector on 4317.
DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"


def setup(
    project_name: str = "selmakit",
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    capture_http: bool = True,
) -> None:
    """Activate OpenTelemetry tracing for the agent, exported over OTLP/HTTP.

    Call once at startup before the gateway starts accepting requests.
    Without this call the tracer is a no-op and the program runs unchanged —
    ``Gateway.serve()`` only calls it when ``tracing.enabled`` is set in
    ``selmakit.json``, since an exporter with no collector listening retries
    every refused connection and logs an error per turn.

    Built on the Logfire SDK, which ships with ``pydantic-ai`` — nothing extra
    to install. Logfire is used purely as an OpenTelemetry client here:
    ``send_to_logfire=False`` means **no data leaves the machine and no Logfire
    account or token is involved**. Spans go to ``endpoint`` only; run any
    OTLP/HTTP collector there. To use Logfire's hosted backend instead,
    configure it yourself rather than calling this function.

    ``capture_http`` additionally records the raw request/response bodies of
    the HTTP calls to the model provider — what actually went over the wire, as
    opposed to pydantic-ai's own view of the run. Headers are deliberately
    *not* captured: they carry the provider API keys.

    Because the export is assumed local, Logfire's value scrubbing is switched off
    — its patterns (``session`` among them) otherwise blank whole system
    messages and make the traces useless for prompt debugging. Spans therefore
    contain prompts, responses and request bodies verbatim; treat the collector
    as as sensitive as the session files on disk, and re-enable scrubbing if
    you point ``endpoint`` at anything remote.

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
        send_to_logfire=False,
        console=False,
        # selmakit never uses Logfire's f-string magic, so skip the source
        # introspection — it warns noisily whenever source is unavailable
        # (interactive shells, exec(), .pyc without .py).
        inspect_arguments=False,
        # Logfire's default scrubber blanks a whole value when it matches one of
        # its patterns — and "session" is one of them, which wipes the entire
        # workspace-files system message (SOUL.md says "Every session you start
        # fresh"). That guts exactly what these traces are for. Turning it off
        # is defensible *because this export is local-only*: send_to_logfire is
        # False, the collector is on localhost, and headers (which carry the
        # provider API keys) are never captured. Re-enable it if you ever point
        # `endpoint` at a remote collector or at Logfire's hosted backend.
        scrubbing=False,
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
