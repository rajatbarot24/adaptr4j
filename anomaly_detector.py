"""
Component 4: Isolation Forest anomaly detector for shadow-mode proactive signals.

During the shadow-mode warm-up (first 30 windows) and continuously thereafter,
an Isolation Forest is trained on the stream of context vectors.  When an
incoming vector is flagged as anomalous the controller can exit shadow mode
early or raise the alert level before an SLO breach occurs.

Reference: Liu et al., "Isolation Forest," ICDM 2008.
"""

import logging
from collections import deque

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger(__name__)


class AnomalyDetector:
    """
    Sliding-window Isolation Forest.

    The model is retrained every `retrain_every` windows on the most recent
    `window_size` context vectors, keeping memory bounded.
    """

    def __init__(
        self,
        window_size: int = 500,
        retrain_every: int = 50,
        contamination: float = 0.05,   # expected fraction of anomalies
        n_estimators: int = 100,
        random_state: int = 42,
        min_samples_to_train: int = 30,
    ):
        self.window_size = window_size
        self.retrain_every = retrain_every
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.min_samples_to_train = min_samples_to_train

        self._buffer: deque[np.ndarray] = deque(maxlen=window_size)
        self._model: IsolationForest | None = None
        self._windows_since_retrain: int = 0
        self._total_windows: int = 0
        self._anomaly_count: int = 0

    # ------------------------------------------------------------------
    def update(self, context: np.ndarray) -> bool:
        """
        Add a new context vector, optionally retrain, and return True if
        the vector is anomalous.
        """
        self._buffer.append(context.copy())
        self._total_windows += 1
        self._windows_since_retrain += 1

        # Retrain on schedule
        if (
            self._windows_since_retrain >= self.retrain_every
            and len(self._buffer) >= self.min_samples_to_train
        ):
            self._retrain()

        return self._is_anomaly(context)

    # ------------------------------------------------------------------
    def _retrain(self) -> None:
        X = np.stack(list(self._buffer))
        self._model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=-1,
        )
        self._model.fit(X)
        self._windows_since_retrain = 0
        log.debug("IsolationForest retrained on %d samples", len(X))

    def _is_anomaly(self, context: np.ndarray) -> bool:
        if self._model is None:
            return False
        # predict returns +1 (normal) or -1 (anomaly)
        pred = self._model.predict(context.reshape(1, -1))[0]
        if pred == -1:
            self._anomaly_count += 1
            return True
        return False

    # ------------------------------------------------------------------
    def anomaly_score(self, context: np.ndarray) -> float:
        """
        Return the raw anomaly score (lower = more anomalous).
        Returns 0.0 if the model is not yet trained.
        """
        if self._model is None:
            return 0.0
        return float(self._model.score_samples(context.reshape(1, -1))[0])

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        return {
            "total_windows":       self._total_windows,
            "buffer_size":         len(self._buffer),
            "anomaly_count":       self._anomaly_count,
            "model_trained":       self._model is not None,
            "windows_since_retrain": self._windows_since_retrain,
        }
