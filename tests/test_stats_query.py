"""Tests for stats_query.py's template and Anthropic query engines.

AnthropicStatsQueryEngine is tested exclusively via FakeAnthropicClient,
never the real `anthropic` package -- this file must pass identically
whether or not `anthropic` is pip-installed in the environment.
"""

from __future__ import annotations

from integration_layer.core.events import AuditEvent
from integration_layer.core.mission import SystemModule
from integration_layer.core.mission_analytics import FleetStats, RobotStats
from integration_layer.core.stats_query import (
    AnthropicStatsQueryEngine,
    TemplateStatsQueryEngine,
    get_query_engine,
)


class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeContentBlock(text)]


class FakeAnthropicClient:
    """Minimal stand-in for anthropic.Anthropic -- used ONLY in tests so
    AnthropicStatsQueryEngine's prompt-construction/response-parsing
    logic can be verified without a real network call or requiring the
    anthropic package. Mimics client.messages.create(...) ->
    response.content[0].text."""

    def __init__(self, scripted_response_text: str):
        self._scripted_response_text = scripted_response_text
        self.messages = self
        self.create_calls = []  # list of kwargs dicts passed to create(), for assertions

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeResponse(self._scripted_response_text)


def test_template_engine_answers_fleet_success_rate():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats(total_missions=4, total_succeeded=3, total_failed=1)
    answer = engine.answer("what's the fleet-wide success rate?", fleet_stats, {}, [])
    assert "75.0%" in answer
    assert "4" in answer


def test_template_engine_identifies_most_reliable_robot():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats()
    robot_stats = {
        "robot-a": RobotStats(robot_id="robot-a", missions_run=10, missions_succeeded=9),
        "robot-b": RobotStats(robot_id="robot-b", missions_run=10, missions_succeeded=3),
    }
    answer = engine.answer("which robot is the most reliable?", fleet_stats, robot_stats, [])
    assert "robot-a" in answer


def test_template_engine_identifies_least_reliable_robot():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats()
    robot_stats = {
        "robot-a": RobotStats(robot_id="robot-a", missions_run=10, missions_succeeded=9),
        "robot-b": RobotStats(robot_id="robot-b", missions_run=10, missions_succeeded=3),
    }
    answer = engine.answer("which robot is the least reliable?", fleet_stats, robot_stats, [])
    assert "robot-b" in answer


def test_template_engine_most_reliable_graceful_when_no_missions_run():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats()
    answer = engine.answer("most reliable robot?", fleet_stats, {}, [])
    assert answer
    assert isinstance(answer, str)


def test_template_engine_answers_failure_breakdown_by_module():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats(
        failures_by_module={SystemModule.COMMS: 3, SystemModule.SENSOR: 1}
    )
    answer = engine.answer("what's the failure breakdown by module?", fleet_stats, {}, [])
    assert "comms" in answer and "3" in answer
    assert "sensor" in answer and "1" in answer


def test_template_engine_answers_retry_count_question():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats()
    robot_stats = {
        "robot-a": RobotStats(robot_id="robot-a", total_retries=2),
        "robot-b": RobotStats(robot_id="robot-b", total_retries=3),
    }
    answer = engine.answer("how many retries happened?", fleet_stats, robot_stats, [])
    assert "5" in answer


def test_template_engine_unrecognized_question_returns_helpful_fallback():
    engine = TemplateStatsQueryEngine()
    fleet_stats = FleetStats()
    answer = engine.answer("what's the weather today?", fleet_stats, {}, [])
    assert answer
    assert "ANTHROPIC_API_KEY" in answer


def test_get_query_engine_returns_template_when_no_api_key_set(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = get_query_engine()
    assert isinstance(engine, TemplateStatsQueryEngine)


def test_get_query_engine_returns_anthropic_when_api_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    result = get_query_engine(client=FakeAnthropicClient("doesn't matter"))
    assert isinstance(result, AnthropicStatsQueryEngine)


def test_anthropic_engine_returns_fake_clients_scripted_response():
    fleet_stats = FleetStats(total_missions=1, total_succeeded=1)
    robot_stats = {"robot-a": RobotStats(robot_id="robot-a", missions_run=1, missions_succeeded=1)}
    engine = AnthropicStatsQueryEngine(
        api_key="fake", client=FakeAnthropicClient("the fleet is doing great")
    )
    answer = engine.answer("how's the fleet doing?", fleet_stats, robot_stats, [])
    assert answer == "the fleet is doing great"


def test_anthropic_engine_prompt_includes_question_and_real_stats():
    fleet_stats = FleetStats(total_missions=42, total_succeeded=40, total_failed=2)
    robot_stats = {
        "robot-a": RobotStats(robot_id="robot-a", missions_run=20, missions_succeeded=19),
    }
    client = FakeAnthropicClient("irrelevant")
    engine = AnthropicStatsQueryEngine(api_key="fake", client=client)
    question = "what is the overall fleet health?"
    engine.answer(question, fleet_stats, robot_stats, [])

    prompt = client.create_calls[0]["messages"][0]["content"]
    assert question in prompt
    assert str(fleet_stats.total_missions) in prompt
