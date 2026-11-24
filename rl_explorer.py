"""
Component 3: Model-based RL adversarial scenario explorer.

Learns a lightweight transition model P(s'|s, a) from observed (state, action,
next_state) triples and uses it to synthetically generate adversarial
fault-injection scenarios that the bandit has not yet encountered.

The generated scenarios are then replayed against the live stack (via
Toxiproxy) to stress-test the current policy portfolio.

Algorithm:
  - Transition model: ensemble of MLPRegressors (Gaussian bootstrap)
  - Adversarial planning: random shooting + CEM (cross-entropy method)
    to find the fault profile that maximises expected SLO violation cost

References:
  Chua et al., "Deep Reinforcement Learning in a Handful of Trials using
  Probabilistic Dynamics Models," NeurIPS 2018.  (PETS)
  Rubinstein, "The Cross-Entropy Method," 2004.
"""

import logging
from dataclasses import dataclass, field

import numpy as np
from sklearn.neural_network import MLPRegressor

log = logging.getLogger(__name__)

# Fault-profile dimensions fed to Toxiproxy via the experiment harness
# [latency_ms, latency_jitter_ms, error_rate_pct]
_FAULT_DIM = 3
_FAULT_LOW  = np.array([0.0,  0.0,  0.0])
_FAULT_HIGH = np.array([2000.0, 500.0, 80.0])


@dataclass
class TransitionBuffer:
    """Circular replay buffer of (state, action, fault_profile, next_state) tuples."""
    capacity: int = 5000
    _states:   list = field(default_factory=list)
    _actions:  list = field(default_factory=list)
    _faults:   list = field(default_factory=list)
    _next:     list = field(default_factory=list)

    def push(
        self,
        state: np.ndarray,
        action: int,
        fault_profile: np.ndarray,
        next_state: np.ndarray,
    ) -> None:
        if len(self._states) >= self.capacity:
            self._states.pop(0);  self._actions.pop(0)
            self._faults.pop(0);  self._next.pop(0)
        self._states.append(state.copy())
        self._actions.append(action)
        self._faults.append(fault_profile.copy())
        self._next.append(next_state.copy())

    def __len__(self) -> int:
        return len(self._states)

    def as_arrays(self):
        X_state  = np.stack(self._states)
        X_action = np.array(self._actions).reshape(-1, 1)
        X_fault  = np.stack(self._faults)
        X        = np.hstack([X_state, X_action, X_fault])
        Y        = np.stack(self._next)
        return X, Y


class RLExplorer:
    """
    Model-based RL adversarial explorer.

    Workflow (called from the policy engine periodically):
      1. buffer.push() after each window
      2. train() every `train_every` windows once buffer has enough data
      3. adversarial_scenario() returns a fault profile to inject next
    """

    def __init__(
        self,
        n_features: int = 6,
        n_ensemble: int = 5,
        cem_samples: int = 200,
        cem_elite_frac: float = 0.1,
        cem_iterations: int = 5,
        train_every: int = 50,
        min_buffer: int = 100,
        random_state: int = 42,
    ):
        self.n_features   = n_features
        self.n_ensemble   = n_ensemble
        self.cem_samples  = cem_samples
        self.cem_elite    = max(1, int(cem_samples * cem_elite_frac))
        self.cem_iters    = cem_iterations
        self.train_every  = train_every
        self.min_buffer   = min_buffer
        self.rng          = np.random.default_rng(random_state)

        self.buffer       = TransitionBuffer()
        self._ensemble: list[MLPRegressor] = []
        self._trained     = False
        self._windows     = 0

    # ------------------------------------------------------------------
    def observe(
        self,
        state: np.ndarray,
        action: int,
        fault_profile: np.ndarray,
        next_state: np.ndarray,
    ) -> None:
        self.buffer.push(state, action, fault_profile, next_state)
        self._windows += 1
        if (
            self._windows % self.train_every == 0
            and len(self.buffer) >= self.min_buffer
        ):
            self.train()

    # ------------------------------------------------------------------
    def train(self) -> None:
        """Fit the ensemble of transition models."""
        X, Y = self.buffer.as_arrays()
        n = len(X)
        self._ensemble = []
        for i in range(self.n_ensemble):
            idx = self.rng.integers(0, n, size=n)  # bootstrap
            m = MLPRegressor(
                hidden_layer_sizes=(64, 64),
                activation="relu",
                max_iter=200,
                random_state=i,
            )
            m.fit(X[idx], Y[idx])
            self._ensemble.append(m)
        self._trained = True
        log.debug("Transition ensemble trained on %d samples", n)

    # ------------------------------------------------------------------
    def adversarial_scenario(self, current_state: np.ndarray, action: int) -> np.ndarray:
        """
        Use CEM to find the fault profile that maximises predicted SLO cost.

        Returns a fault profile vector: [latency_ms, jitter_ms, error_rate_pct].
        Falls back to a random high-stress profile if the model is not trained.
        """
        if not self._trained:
            return self._random_fault()

        # CEM: initialise with uniform distribution over fault space
        mu  = (_FAULT_LOW + _FAULT_HIGH) / 2.0
        std = (_FAULT_HIGH - _FAULT_LOW) / 4.0

        for _ in range(self.cem_iters):
            samples = self.rng.normal(mu, std, size=(self.cem_samples, _FAULT_DIM))
            samples = np.clip(samples, _FAULT_LOW, _FAULT_HIGH)

            costs = np.array([
                self._predict_cost(current_state, action, f)
                for f in samples
            ])

            elite_idx = np.argsort(costs)[-self.cem_elite:]  # highest cost = worst for SLO
            elite = samples[elite_idx]
            mu  = elite.mean(axis=0)
            std = elite.std(axis=0) + 1e-6

        return mu

    # ------------------------------------------------------------------
    def _predict_cost(
        self, state: np.ndarray, action: int, fault: np.ndarray
    ) -> float:
        """Average ensemble prediction of p99_ms (index 0 of next_state)."""
        x = np.hstack([state, [action], fault]).reshape(1, -1)
        preds = [m.predict(x)[0] for m in self._ensemble]
        next_state_mean = np.mean(preds, axis=0)
        # cost = normalised predicted p99 (index 0 is p99_ms / slo_p99_ms)
        return float(next_state_mean[0])

    def _random_fault(self) -> np.ndarray:
        return self.rng.uniform(_FAULT_LOW, _FAULT_HIGH)

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        return {
            "buffer_size": len(self.buffer),
            "trained":     self._trained,
            "windows":     self._windows,
        }
