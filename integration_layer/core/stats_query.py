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
import time
from typing import Protocol

from integration_layer.core import retry_policy
from integration_layer.core.events import AuditEvent
from integration_layer.core.mission import FailureReason
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
    in _call_api() so tests can substitute a fake client.

    Model-serving hardening, all reusing machinery already proven
    elsewhere in this codebase rather than inventing new patterns:
    - A bounded per-request timeout (request_timeout_s) so a hung API
      call can never block a Flask request thread indefinitely.
    - Retry-with-backoff on failure, via the SAME retry_policy.py COMMS
      table (strategy_for/backoff_for_attempt) that governs mission-step
      and webhook-delivery retries -- an LLM call failing transiently is
      modeled as the same kind of comms failure, not a bespoke retry loop.
      poll_interval_s gates whether backoff actually sleeps (default 0.0,
      same idiom as StepRunner/DeliveryService) so the test suite stays
      fast by default while production can opt into real delays.
    - Graceful degradation: if every retry attempt fails, falls back to
      TemplateStatsQueryEngine rather than raising -- the caller always
      gets a useful answer, never a 500. last_call_used_fallback is set
      so a caller (the webapp route) can report the degraded engine
      honestly instead of silently claiming "anthropic" answered.
    - A small in-memory cache keyed on (question, a cheap signature of
      the stats snapshot) avoids paying for a duplicate call when the
      same question is asked again against unchanged data -- the
      signature changes the instant any mission completes, so a cached
      answer is never served once the underlying data has moved on.
      Fallback answers are deliberately never cached, so a transient
      outage can't poison later responses once the API recovers.

    Explicitly NOT built, as a documented scope boundary consistent with
    this repo's "what's real vs. illustrative" practice: no cross-request
    rate limiting or spend cap on the /api/stats/query endpoint, and no
    model routing beyond the template-vs-LLM split that already exists
    (TemplateStatsQueryEngine handles the common questions for free and
    with zero latency; the LLM is only invoked for genuinely open-ended
    ones). Streaming isn't implemented either -- answers are capped at
    1-3 sentences by the prompt itself, so there's nothing to stream."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5",
        client=None,
        request_timeout_s: float = 10.0,
        poll_interval_s: float = 0.0,
    ) -> None:
        self._model = model
        self._request_timeout_s = request_timeout_s
        self._poll_interval_s = poll_interval_s
        self._fallback = TemplateStatsQueryEngine()
        self._cache: dict[tuple, str] = {}
        self.last_call_used_fallback = False
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
        self.last_call_used_fallback = False
        cache_key = self._cache_key(question, fleet_stats, robot_stats)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        prompt = self._build_prompt(question, fleet_stats, robot_stats, recent_events)
        try:
            answer = self._call_api_with_retry(prompt)
        except Exception:
            self.last_call_used_fallback = True
            return self._fallback.answer(question, fleet_stats, robot_stats, recent_events)

        self._cache[cache_key] = answer
        return answer

    def _cache_key(
        self, question: str, fleet_stats: FleetStats, robot_stats: dict[str, RobotStats]
    ) -> tuple:
        """Cheap, deterministic signature of the stats snapshot -- not a
        hash of everything, just enough fields that any mission outcome
        changes it, so a cached answer can never be served once the data
        it was computed against is stale."""
        robot_signature = tuple(
            sorted((rid, s.missions_run, s.missions_succeeded, s.total_retries) for rid, s in robot_stats.items())
        )
        return (
            question.strip().lower(),
            fleet_stats.total_missions,
            fleet_stats.total_succeeded,
            fleet_stats.total_failed,
            robot_signature,
        )

    def _call_api_with_retry(self, prompt: str) -> str:
        """Retries via the SAME COMMS RetryStrategy (max_attempts=4,
        backoff_s=(0.0, 1.0, 2.0)) that governs mission-step and
        webhook-delivery retries elsewhere in this codebase -- an LLM
        call failing is treated as a comms failure, not a reason to
        invent a second retry table. Catches a broad Exception (rather
        than specific anthropic.* exception classes) deliberately: this
        module imports `anthropic` lazily and never at module level, and
        tests inject fake clients that may raise plain built-in
        exceptions to simulate failure -- narrowing the catch to real SDK
        exception types would require a hard import this module is
        designed to avoid."""
        strategy = retry_policy.strategy_for(FailureReason.CONNECTIVITY_LOST)  # COMMS strategy
        attempt = 1
        while True:
            try:
                return self._call_api(prompt)
            except Exception:
                if attempt >= strategy.max_attempts:
                    raise
                backoff = retry_policy.backoff_for_attempt(strategy, attempt + 1)
                if self._poll_interval_s and backoff:
                    time.sleep(backoff)
                attempt += 1

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
        # max_tokens is sized well above the ~512 tokens the answer itself
        # needs: on Claude Sonnet 5 (the default model string here),
        # omitting `thinking` runs adaptive thinking by default, and that
        # thinking spend is drawn from this same max_tokens budget. A
        # tight budget risks truncating -- or fully starving -- the
        # answer text. 1500 leaves comfortable headroom for both.
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            timeout=self._request_timeout_s,
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
