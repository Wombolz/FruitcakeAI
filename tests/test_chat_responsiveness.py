from __future__ import annotations

from unittest.mock import patch

from app.api.chat import _record_chat_stage_timing
from app.metrics import _Metrics


def test_record_chat_stage_timing_records_elapsed_ms():
    stage_timings = {}
    with patch("app.api.chat.metrics", new=_Metrics()):
        with patch("app.api.chat.time.perf_counter", return_value=10.025):
            _record_chat_stage_timing(stage_timings, "history_load", 10.0)
        assert stage_timings["history_load"] == 25.0
