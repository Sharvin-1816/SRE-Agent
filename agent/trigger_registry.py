"""
agent/trigger_registry.py

Single shared cooldown registry for deciding whether the agent should
run a full analysis (RCA -> prediction -> blast radius -> alerts) for a
given service right now, or whether one already ran recently enough
that running again would just be redundant.

WHY THIS EXISTS
----------------
There are two independent paths that can trigger the agent for the same
service at nearly the same moment:

  1. collector.py's scheduled poll, when it detects an anomaly directly
     from the mock services' metrics (trigger="anomaly_detected")
  2. api/webhook_receiver.py, when Grafana fires an alert for that same
     underlying condition (trigger="webhook_grafana")

Before this module existed, each of those two files kept its OWN
in-memory cooldown dictionary — collector.py's `_cooldown` and
webhook_receiver.py's `_last_triggered` — with no knowledge of each
other. In practice, a single real incident (e.g. inventory_service
running out of memory) produces both a collector-detected anomaly AND
a Grafana webhook alert within the same minute. Each file's cooldown
check only looked at ITS OWN trigger history, saw nothing recent, and
both independently decided "no prior trigger, go ahead" — resulting in
two full, simultaneous, independent agent analyses for the exact same
incident: two parallel sets of RCA/prediction/blast-radius/alert-
grouping LLM calls, racing each other, each storing its own slightly
different memory pattern for what is actually one single event.

This module is the one shared registry both files now consult, so
whichever trigger path fires FIRST puts the service into cooldown for
both paths simultaneously.

USAGE
-----
    from agent.trigger_registry import should_trigger, record_trigger

    suppressed, reason = should_trigger(service_name, score)
    if not suppressed:
        record_trigger(service_name, score)
        # ... run the agent ...
"""

import os
from datetime import datetime, timezone

# Minutes a service stays in cooldown after the agent analyses it,
# regardless of which path (collector or webhook) triggered the run.
COOLDOWN_MINUTES = int(os.getenv("AGENT_COOLDOWN_MINUTES", "10"))

# Score at which the agent triggers regardless of cooldown (genuine
# escalation — e.g. a service going from degraded to fully down).
# Webhook-sourced triggers don't carry a real anomaly score (there's no
# z-score to compute from a Grafana alert payload), so they're treated
# as score=50 by convention — see record_trigger()'s docstring below.
AGENT_ALWAYS_SCORE = float(os.getenv("AGENT_ALWAYS_SCORE", "80"))

# Key: service_name  Value: (last_triggered_at, last_score)
_cooldown: dict[str, tuple[datetime, float]] = {}


def should_trigger(service_name: str, current_score: float) -> tuple[bool, str]:
    """
    Returns (suppressed: bool, reason: str).

    Cooldown is overridden when:
      - No prior trigger exists for this service
      - Cooldown window has fully elapsed
      - Score exceeds AGENT_ALWAYS_SCORE (genuine escalation)
      - Score has increased by more than 20 points since last trigger
        (situation is getting worse, not stable)
    """
    entry = _cooldown.get(service_name)
    if not entry:
        return False, "no prior trigger"

    last_triggered, last_score = entry
    elapsed_minutes = (datetime.now(timezone.utc) - last_triggered).total_seconds() / 60

    if elapsed_minutes >= COOLDOWN_MINUTES:
        return False, f"cooldown expired ({elapsed_minutes:.1f} min ago)"

    if current_score >= AGENT_ALWAYS_SCORE:
        return False, f"score {current_score} exceeds always-trigger threshold ({AGENT_ALWAYS_SCORE})"

    if current_score - last_score >= 20:
        return False, f"severity escalated ({last_score} → {current_score})"

    remaining = COOLDOWN_MINUTES - elapsed_minutes
    return True, f"cooldown active — {remaining:.1f} min remaining (last score: {last_score})"


def record_trigger(service_name: str, score: float = 50.0):
    """
    Record that the agent was triggered for this service, starting (or
    restarting) its cooldown window.

    `score` defaults to 50.0 for callers that don't have a real
    computed anomaly score available — specifically webhook_receiver.py,
    which triggers from a Grafana alert payload rather than a z-score
    computation. 50.0 sits comfortably below AGENT_ALWAYS_SCORE (80) so
    a webhook-sourced trigger doesn't itself bypass future cooldowns,
    while still being a reasonable mid-severity placeholder for the
    "severity escalated by 20+" check above.
    """
    _cooldown[service_name] = (datetime.now(timezone.utc), score)


def active_cooldowns() -> dict[str, tuple[datetime, float]]:
    """Read-only view of the current registry, for status/debug display."""
    return dict(_cooldown)
