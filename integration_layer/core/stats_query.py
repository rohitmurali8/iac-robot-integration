"""LLM-powered natural-language querying over fleet/robot statistics,
with a safe, always-available fallback.

TemplateStatsQueryEngine answers common question shapes (success rate,
most/least reliable robot, failure breakdown by module, retry counts,
total mission count) via deterministic keyword matching -- no network,
no API key, always available. AnthropicStatsQueryEngine answers ANY
question via a real call to the Anthropic Messages API, used only when
ANTHROPIC_API_KEY is set.

The `anthropic` package is imported LAZILY, inside
AnthropicStatsQueryEngine.__init__, and only when no `client` override
is supplied -- every test in this module passes an explicit fake client,
so this module's own test suite never requires `anthropic` to be
installed. Real production use (no client override) does require
`pip install anthropic`.

AnthropicStatsQueryEngine's actual network call is isolated in
_call_api() specifically so tests can substitute a fake client --
the one deliberate exception to this repo's "hit real dependencies,
don't mock" testing philosophy elsewhere, justified because a real LLM
call costs money per invocation and must never run automatically in a
test suite.
"""

from __future__ import annotations

import os
from typing import Protocol

from integration_layer.core.events import AuditEvent
from integration_layer.core.mission_analytics import FleetStats, RobotStats


class StatsQueryEngine(Protocol):
    def answer(
        self,
        question: str,
        fleet_stats: FleetStats,
        robot_stats: dict[str, RobotStats],
        recent_events: list[AuditEvent],
    ) -> str: ...


class TemplateStatsQueryEngine:
    """No network, no API key. Pattern-matches common question shapes
    directly against the stats snapshot. Always available -- the
    default when no LLM is configured."""

    def answer(
        self,
        question: str,
        fleet_stats: FleetStats,
        robot_stats: dict[str, RobotStats],
        recent_events: list[AuditEvent],
    ) -> str:
        q = question.lower()

        if "success rate" in q and ("fleet" in q or "overall" in q):
            return (
                f"Fleet-wide success rate is {fleet_stats.overall_success_rate:.1%} "
                f"across {fleet_stats.total_missions} missions."
            )

        if "most reliable" in q or "best robot" in q:
            robot_id, stats = self._best(robot_stats)
            if robot_id is None:
                return "No robot has completed a mission yet."
            return (
                f"{robot_id} is the most reliable robot, with a success rate of "
                f"{stats.success_rate:.1%} over {stats.missions_run} missions."
            )

        if "least reliable" in q or "worst robot" in q:
            robot_id, stats = self._worst(robot_stats)
            if robot_id is None:
                return "No robot has completed a mission yet."
            return (
                f"{robot_id} is the least reliable robot, with a success rate of "
                f"{stats.success_rate:.1%} over {stats.missions_run} missions."
            )

        if "module" in q and ("fail" in q or "breakdown" in q):
            if not fleet_stats.failures_by_module:
                return "No failures recorded yet."
            parts = ", ".join(f"{module.value}: {count}" for module, count in fleet_stats.failures_by_module.items())
            return f"Failures by system module -- {parts}."

        if "retr" in q:
            total = sum(s.total_retries for s in robot_stats.values())
            return f"{total} total retry attempts recorded across the fleet."

        if "how many mission" in q or "total mission" in q:
            return f"{fleet_stats.total_missions} total missions have been run."

        return (
            "I can answer fleet success rate, most/least reliable robot, "
            "failure breakdown by module, retry count, and total mission count "
            "questions. For anything else, configure ANTHROPIC_API_KEY for full "
            "natural-language querying."
        )

    def _best(self, robot_stats: dict[str, RobotStats]):
        candidates = [(rid, s) for rid, s in robot_stats.items() if s.missions_run > 0]
        if not candidates:
            return None, None
        return max(candidates, key=lambda pair: pair[1].success_rate)

    def _worst(self, robot_stats: dict[str, RobotStats]):
        candidates = [(rid, s) for rid, s in robot_stats.items() if s.missions_run > 0]
        if not candidates:
            return None, None
        return min(candidates, key=lambda pair: pair[1].success_rate)


class AnthropicStatsQueryEngine:
    """Real natural-language querying via the Anthropic Messages API.
    Requires `pip install anthropic` and ANTHROPIC_API_KEY for real use
    (client=None, the production path). The actual HTTP call is isolated
    in _call_api() so tests can substitute a fake client."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5", client=None) -> None:
        self._model = model
        if client is not None:
            self._client = client
        else:
            import anthropic  # lazy: only required when no fake client is injected

            self._client = anthropic.Anthropic(api_key=api_key)

    def answer(
        self,
        question: str,
        fleet_stats: FleetStats,
        robot_stats: dict[str, RobotStats],
        recent_events: list[AuditEvent],
    ) -> str:
        prompt = self._build_prompt(question, fleet_stats, robot_stats, recent_events)
        return self._call_api(prompt)

    def _build_prompt(
        self,
        question: str,
        fleet_stats: FleetStats,
        robot_stats: dict[str, RobotStats],
        recent_events: list[AuditEvent],
    ) -> str:
        lines = [
            "You are answering a question about a robot fleet's mission "
            "performance, using only the data below. Be concise -- 1-3 sentences.",
            "",
            f"Fleet-wide: {fleet_stats.total_missions} missions run, "
            f"{fleet_stats.total_succeeded} succeeded, {fleet_stats.total_failed} failed, "
            f"{fleet_stats.total_paused} paused for intervention. "
            f"Overall success rate: {fleet_stats.overall_success_rate:.1%}.",
            f"Failures by system module: {dict(fleet_stats.failures_by_module)}.",
            "",
            "Per-robot stats:",
        ]
        for robot_id, stats in robot_stats.items():
            lines.append(
                f"- {robot_id}: {stats.missions_run} missions run, "
                f"success rate {stats.success_rate:.1%}, "
                f"{stats.total_retries} total retries "
                f"(by module: {dict(stats.retries_by_module)})."
            )
        lines.append("")
        lines.append(f"Recent audit events (most recent {len(recent_events)}):")
        for event in recent_events:
            lines.append(
                f"- [{event.timestamp_unix_ms}] mission={event.mission_id} "
                f"robot={event.robot_id} {event.event_type}: {event.detail}"
            )
        lines.append("")
        lines.append(f"Question: {question}")
        return "\n".join(lines)

    def _call_api(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


def get_query_engine(client=None) -> StatsQueryEngine:
    """ANTHROPIC_API_KEY set -> AnthropicStatsQueryEngine; else ->
    TemplateStatsQueryEngine. The optional client param exists purely
    for testability (see AnthropicStatsQueryEngine's own client param)
    -- real callers never pass it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return AnthropicStatsQueryEngine(api_key=api_key, client=client)
    return TemplateStatsQueryEngine()
