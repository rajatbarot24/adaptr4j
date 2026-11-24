"""
Component 2: LinUCB Contextual Bandit controller.

Selects among the discrete policy menu based on the current system context vector.
Uses the upper confidence bound (UCB) strategy for exploration/exploitation balance.

Reference: Li et al., "A Contextual-Bandit Approach to Personalized News Article
Recommendation," WWW 2010.
"""

import numpy as np
from dataclasses import dataclass, field


@dataclass
class LinUCBBandit:
    n_actions: int
    n_features: int
    alpha: float = 1.0             # exploration parameter
    alpha_min: float = 0.5         # floor after warm-up
    warmup_windows: int = 100      # windows before reducing alpha

    # Per-action ridge regression matrices (initialized to identity)
    A: list = field(default_factory=list)   # A_a: n_features x n_features
    b: list = field(default_factory=list)   # b_a: n_features

    total_updates: int = 0

    # Safe exploration (v3): an arm whose recent mean reward is more than
    # `safe_margin` below the best arm's is not selected again until it has
    # rested `readmit_after` windows (so it can be re-tested after a fault change).
    safe_explore: bool = False
    safe_margin: float = 1.0
    readmit_after: int = 30
    recent_k: int = 5

    def __post_init__(self):
        self.A = [np.eye(self.n_features) for _ in range(self.n_actions)]
        self.b = [np.zeros(self.n_features) for _ in range(self.n_actions)]
        self._recent = [[] for _ in range(self.n_actions)]
        self._last_pull = [-10 ** 9] * self.n_actions

    def best_known(self) -> int | None:
        """Arm with the highest recent mean reward, or None before any update."""
        means = {a: float(np.mean(r)) for a, r in enumerate(self._recent) if r}
        return max(means, key=means.get) if means else None

    def eligible(self) -> list[int]:
        if not self.safe_explore:
            return list(range(self.n_actions))
        means = {a: float(np.mean(r)) for a, r in enumerate(self._recent) if r}
        if not means:
            return list(range(self.n_actions))
        best = max(means.values())
        t = self.total_updates
        ok = [a for a in range(self.n_actions)
              if a not in means
              or means[a] >= best - self.safe_margin
              or t - self._last_pull[a] >= self.readmit_after]
        return ok or list(range(self.n_actions))

    @property
    def current_alpha(self) -> float:
        """Decay exploration parameter after warm-up phase."""
        if self.total_updates >= self.warmup_windows:
            return self.alpha_min
        progress = self.total_updates / self.warmup_windows
        return self.alpha - (self.alpha - self.alpha_min) * progress

    def select_action(self, context: np.ndarray) -> tuple[int, np.ndarray]:
        """
        Select the action with the highest UCB score.
        Returns (action_index, ucb_scores_for_all_actions).
        """
        assert context.shape == (self.n_features,), \
            f"Expected context shape ({self.n_features},), got {context.shape}"

        alpha = self.current_alpha
        # Ineligible arms get a large finite negative score (not -inf: the decision
        # logger writes strict JSON, which has no infinity).
        ucb_scores = np.full(self.n_actions, -1e9)

        for a in self.eligible():
            theta_a = np.linalg.solve(self.A[a], self.b[a])
            exploit = theta_a @ context
            explore = alpha * np.sqrt(context @ np.linalg.solve(self.A[a], context))
            ucb_scores[a] = exploit + explore

        return int(np.argmax(ucb_scores)), ucb_scores

    def update(self, action: int, context: np.ndarray, reward: float) -> None:
        """Update the ridge regression model for the chosen action."""
        self.A[action] += np.outer(context, context)
        self.b[action] += reward * context
        self._recent[action] = (self._recent[action] + [reward])[-self.recent_k:]
        self._last_pull[action] = self.total_updates
        self.total_updates += 1

    def state_dict(self) -> dict:
        """Serialize bandit state for checkpointing."""
        return {
            "A": [a.tolist() for a in self.A],
            "b": [b.tolist() for b in self.b],
            "total_updates": self.total_updates,
            "alpha": self.alpha,
        }

    def load_state_dict(self, state: dict) -> None:
        """Restore bandit state from checkpoint."""
        self.A = [np.array(a) for a in state["A"]]
        self.b = [np.array(b) for b in state["b"]]
        self.total_updates = state["total_updates"]
        self.alpha = state["alpha"]
