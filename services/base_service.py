"""
services/base_service.py

Base class for all mock microservices.
Each service inherits this and just defines its own baseline characteristics.

Every service exposes:
    GET /health   → simple up/down check
    GET /metrics  → full metrics snapshot
    POST /degrade → manually trigger degradation (for demos)
    POST /recover → restore to normal
    POST /surge   → simulate traffic surge
"""

import random
import time
import math
from datetime import datetime, timezone
from fastapi import FastAPI
from pydantic import BaseModel


class DegradationConfig(BaseModel):
    intensity: float = 0.5      # 0.0 to 1.0
    duration_seconds: int = 120


class BaseService:
    def __init__(
        self,
        name: str,
        base_rt: float,       # baseline response time ms
        base_er: float,       # baseline error rate %
        base_tp: float,       # baseline throughput rps
        base_cpu: float,
        base_mem: float,
        rt_noise: float,      # std dev of normal noise
        er_noise: float,
        tp_noise: float,
    ):
        self.name     = name
        self.base_rt  = base_rt
        self.base_er  = base_er
        self.base_tp  = base_tp
        self.base_cpu = base_cpu
        self.base_mem = base_mem
        self.rt_noise = rt_noise
        self.er_noise = er_noise
        self.tp_noise = tp_noise

        # State flags
        self._degrading    = False
        self._intensity    = 0.0
        self._surge        = False
        self._surge_mult   = 1.0
        self._start_time   = time.time()
        self._request_count = 0

        self.app = FastAPI(title=name)
        self._register_routes()

    # ── Route registration ────────────────────────────────────────────────────

    def _register_routes(self):

        @self.app.get("/health")
        def health():
            if self._degrading and self._intensity > 0.85:
                return {"status": "unhealthy", "service": self.name}
            return {"status": "healthy", "service": self.name}

        @self.app.get("/metrics")
        def metrics():
            self._request_count += 1
            return self._generate_metrics()

        @self.app.post("/degrade")
        def degrade(config: DegradationConfig):
            self._degrading  = True
            self._intensity  = max(0.0, min(1.0, config.intensity))
            return {
                "status":    "degradation started",
                "intensity": self._intensity,
                "service":   self.name,
            }

        @self.app.post("/recover")
        def recover():
            self._degrading = False
            self._intensity = 0.0
            self._surge     = False
            self._surge_mult = 1.0
            return {"status": "recovered", "service": self.name}

        @self.app.post("/surge")
        def surge(multiplier: float = 4.0):
            self._surge      = True
            self._surge_mult = multiplier
            return {
                "status":     "surge started",
                "multiplier": multiplier,
                "service":    self.name,
            }

    # ── Metric generation ─────────────────────────────────────────────────────

    def _load_curve_multiplier(self) -> float:
        """Simulate realistic time-of-day load variation."""
        hour = datetime.now(timezone.utc).hour
        curve = [
            0.3, 0.2, 0.2, 0.2, 0.2, 0.3,
            0.5, 0.7, 0.9, 1.0, 1.0, 1.0,
            1.1, 1.1, 1.0, 1.0, 1.1, 1.2,
            1.3, 1.2, 1.0, 0.8, 0.6, 0.4,
        ]
        return curve[hour]

    def _noisy(self, base: float, noise: float, mult: float = 1.0) -> float:
        return round(max(0.0, base * mult + random.gauss(0, noise)), 2)

    def _clamp(self, v: float, lo: float, hi: float) -> float:
        return round(max(lo, min(hi, v)), 2)

    def _generate_metrics(self) -> dict:
        load   = self._load_curve_multiplier()
        surge  = self._surge_mult if self._surge else 1.0
        intens = self._intensity  if self._degrading else 0.0

        if self._degrading:
            # Degradation: RT spikes, errors rise, throughput drops
            rt  = self._noisy(
                self.base_rt * (1 + intens * 12),
                self.rt_noise * (1 + intens * 3)
            )
            er  = self._clamp(
                self._noisy(self.base_er * (1 + intens * 10), self.er_noise * 3),
                0, 100
            )
            tp  = self._noisy(
                self.base_tp * max(0.05, 1 - intens * 0.85),
                self.tp_noise
            )
            cpu = self._clamp(
                self._noisy(self.base_cpu * (1 + intens * 0.8), 5),
                0, 100
            )
            mem = self._clamp(
                self._noisy(self.base_mem * (1 + intens * 0.5), 4),
                0, 100
            )
            uptime = self._clamp(100 - intens * 40, 0, 100)

        elif self._surge:
            # Surge: high throughput, RT increases, slight error bump
            rt  = self._noisy(self.base_rt  * (1 + (surge - 1) * 0.4), self.rt_noise * 2)
            er  = self._clamp(
                self._noisy(self.base_er * (1 + (surge - 1) * 0.2), self.er_noise),
                0, 100
            )
            tp  = self._noisy(self.base_tp  * surge, self.tp_noise * surge * 0.3)
            cpu = self._clamp(
                self._noisy(self.base_cpu * (1 + (surge - 1) * 0.5), 5),
                0, 100
            )
            mem = self._clamp(
                self._noisy(self.base_mem * (1 + (surge - 1) * 0.2), 3),
                0, 100
            )
            uptime = 100.0

        else:
            # Normal operation
            rt  = self._noisy(self.base_rt,  self.rt_noise,  load)
            er  = self._clamp(
                self._noisy(self.base_er, self.er_noise, load * 0.3),
                0, 100
            )
            tp  = self._noisy(self.base_tp,  self.tp_noise,  load)
            cpu = self._clamp(
                self._noisy(self.base_cpu, self.rt_noise * 0.1, load),
                0, 100
            )
            mem = self._clamp(self._noisy(self.base_mem, 3), 0, 100)
            uptime = 100.0

        return {
            "service":        self.name,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "response_time_ms": rt,
            "error_rate_pct":   er,
            "throughput_rps":   tp,
            "uptime_pct":       uptime,
            "upload_time_ms":   round(rt * random.uniform(0.3, 0.6), 2),
            "cpu_pct":          cpu,
            "memory_pct":       mem,
            "is_degrading":     self._degrading,
            "is_surging":       self._surge,
            "request_count":    self._request_count,
        }
