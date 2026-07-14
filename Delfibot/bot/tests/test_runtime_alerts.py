import asyncio
import sys
from pathlib import Path


BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from engine import runtime_alerts
from engine.llm_client import LLMClient


def test_runtime_alerts_notify_once_and_report_recovery(monkeypatch):
    events = []
    runtime_alerts._ACTIVE_FAILURES.clear()
    monkeypatch.setattr(
        runtime_alerts,
        "log_event",
        lambda **event: events.append(event),
    )

    runtime_alerts.report_failure("forecast_provider", "Every provider failed.")
    runtime_alerts.report_failure("forecast_provider", "Every provider failed.")
    runtime_alerts.report_recovery("forecast_provider")
    runtime_alerts.report_recovery("forecast_provider")

    assert [event["severity"] for event in events] == [30, 10]
    assert all(event["event_type"] == "trading_blocked" for event in events)
    assert "No new positions can open" in events[0]["description"]
    assert "recovered" in events[1]["description"]


def test_forecaster_failure_and_recovery_drive_runtime_alerts(monkeypatch):
    from engine import llm_client

    failures = []
    recoveries = []
    monkeypatch.setattr(llm_client, "resolve_llm_chain", lambda _use_case: [])
    monkeypatch.setattr(
        runtime_alerts,
        "report_failure",
        lambda kind, detail: failures.append((kind, detail)),
    )
    monkeypatch.setattr(runtime_alerts, "report_recovery", recoveries.append)

    client = LLMClient()
    assert asyncio.run(client.call(
        system=None,
        user="test",
        max_tokens=10,
        use_case="forecaster",
    )) is None

    async def successful_forecast(*_args, **_kwargs):
        return "forecast"

    monkeypatch.setattr(
        llm_client,
        "resolve_llm_chain",
        lambda _use_case: [{"provider": "anthropic"}],
    )
    monkeypatch.setattr(client, "_call_anthropic", successful_forecast)
    assert asyncio.run(client.call(
        system=None,
        user="test",
        max_tokens=10,
        use_case="forecaster",
    )) == "forecast"

    assert failures == [("forecast_provider", "No forecast provider is configured.")]
    assert recoveries == ["forecast_provider"]
