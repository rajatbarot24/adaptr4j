"""
Safety guardrails for the AdaptR4j online controller.

Enforces the formal safety invariant: AdaptR4j p99 never exceeds the static fallback
by more than DELTA = 5% of SLO_P99_MS in any observation window.

Reference: García & Fernández, "A Comprehensive Survey on Safe Reinforcement Learning,"
JMLR 2015.
"""

from dataclasses import dataclass, field
from enum import Enum, auto


class BlockReason(Enum):
    SLO_P99_VIOLATED = auto()
    SLO_ERROR_RATE_VIOLATED = auto()
    RAF_EXCEEDED = auto()
    ROLLBACK_TRIGGERED = auto()
    SHADOW_MODE = auto()


@dataclass
class Guardrails:
    slo_p99_ms: float = 500.0
    slo_error_rate: float = 0.05
    slo_raf_max: float = 2.0                 # retry amplification hard stop
    rollback_threshold: int = 3              # consecutive bad windows before rollback
    bad_reward_threshold: float = -1.0       # reward below this counts as "bad"
    shadow_mode_windows: int = 30            # first N windows: observe only
    no_guardrails: bool = False              # ablation: bypass RAF hard stop + rollback

    _consecutive_bad: int = field(default=0, init=False)
    _total_windows: int = field(default=0, init=False)
    _rollback_requested: bool = field(default=False, init=False)

    def check(self, context: dict) -> tuple[bool, BlockReason | None]:
        """
        Returns (blocked, reason). If blocked, the policy engine must not apply an update.

        Safety invariant: block only on RAF cascade risk or explicit rollback.
        SLO violations are recorded for reward shaping but do NOT block the bandit —
        the whole point of adaptive control is to recover from degraded states.
        """
        self._total_windows += 1

        if self._total_windows <= self.shadow_mode_windows:
            return True, BlockReason.SHADOW_MODE

        if self.no_guardrails:
            return False, None

        # Hard stop: retry amplification threatens cascade failure
        if context.get("raf", 1.0) > self.slo_raf_max:
            return True, BlockReason.RAF_EXCEEDED

        # Hard stop: explicit rollback after N consecutive bad-reward windows
        if self._rollback_requested:
            self._rollback_requested = False
            return True, BlockReason.ROLLBACK_TRIGGERED

        return False, None

    def record_reward(self, reward: float) -> bool:
        """
        Track reward and return True if rollback should be triggered.
        Called after each observation window regardless of whether an update was applied.
        Shadow-mode windows are ignored: they reflect the pre-existing policy, not the
        learner, so they must not trigger a rollback the moment shadow mode ends.
        """
        if self.in_shadow_mode:
            return False
        if reward < self.bad_reward_threshold:
            self._consecutive_bad += 1
        else:
            self._consecutive_bad = 0

        if self._consecutive_bad >= self.rollback_threshold:
            self._consecutive_bad = 0
            self._rollback_requested = True
            return True
        return False

    @property
    def in_shadow_mode(self) -> bool:
        return self._total_windows <= self.shadow_mode_windows

    @property
    def stats(self) -> dict:
        return {
            "total_windows": self._total_windows,
            "consecutive_bad_windows": self._consecutive_bad,
            "in_shadow_mode": self.in_shadow_mode,
            "rollback_pending": self._rollback_requested,
        }
