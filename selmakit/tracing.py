import logging

logger = logging.getLogger(__name__)


def setup(project_name: str = "selmakit", endpoint: str | None = None) -> None:
    """Activate Phoenix tracing with pydantic-ai's built-in OTel instrumentation.

    Call once at startup before the gateway starts accepting requests.
    Without this call the tracer is a no-op and the program runs unchanged.

    If Phoenix is missing or incompatible (e.g. arize-phoenix not yet
    released for pydantic-ai 2.x), tracing is skipped and a warning is logged
    — the gateway continues without tracing.
    """
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    try:
        from phoenix.otel import register
        from pydantic_ai import Agent
        from pydantic_ai.agent import InstrumentationSettings
    except ImportError as e:
        logger.warning(
            "Phoenix tracing disabled — incompatible with current pydantic-ai (%s)", e
        )
        return

    kwargs: dict = {"project_name": project_name}
    if endpoint:
        kwargs["endpoint"] = endpoint
    register(**kwargs)
    Agent.instrument_all(InstrumentationSettings(include_content=True))
