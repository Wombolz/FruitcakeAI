import structlog

from app.logging_config import configure_application_logging


def test_application_tracebacks_do_not_render_frame_locals():
    configure_application_logging()

    renderer = structlog.get_config()["processors"][-1]
    formatter = renderer._exception_formatter

    assert isinstance(formatter, structlog.dev.RichTracebackFormatter)
    assert formatter.show_locals is False
