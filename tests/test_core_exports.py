"""Tests for lazy exports from symbio.core."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio import core


def test_core_exports_planner_reviewer_and_agent_handoff():
    assert core.PlannerReviewerLoop.__name__ == "PlannerReviewerLoop"
    assert core.ReviewFindingSeverity.__name__ == "ReviewFindingSeverity"
    assert core.AgentHandoff.__name__ == "AgentHandoff"
