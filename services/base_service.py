"""
services/base_service.py

Base class for all mock microservices.
Each service inherits this and defines its own baseline characteristics.

Every service exposes:
    GET /health   → simple up/down check
    GET /metrics  → Prometheus-format metrics (scraped by Prometheus)
    GET /metrics/json → legacy JSON format (used by collector fallback)
    POST /degrade → manually trigger degradation
    POST /recover → restore to normal
    POST /surge   → simulate traffic surge
"""

import random
import time
import math
from datetime import datetime, timezone
from fastapi import FastAPI, Response
from pydantic import BaseModel
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST,
    CollectorRegistry
)


class DegradationConfig(BaseModel):
    intensity: float = 0.5
    duration_seconds: int = 120


class BaseService:
    def __init__(
        self,
        name: str,
        base_rt: float,
        base_er: float,
        base_tp: float,
        base_cpu: float,
        base_mem: float,
        rt_noise: float,
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
        self._degrading     = False
        self._intensity     = 0.0
        self._surge         = False
        self._surge_mult    = 1.0
        self._start_time    = time.time()
        self._request_count = 0

        # ── Prometheus metrics (one registry per service instance) ──────────
        self._registry = CollectorRegistry()

        self._requests_total = Counter(
            "sre_requests_total",
            "Total number of requests",
            ["service_name"],
            registry=self._registry,
        )
        self._request_duration = Histogram(
            "sre_request_duration_ms",
            "Request duration in milliseconds",
            ["service_name"],
            buckets=[50, 100, 200, 400, 600, 800, 1000,
                     1500, 2000, 3000, 5000, 8000, 10000],
            registry=self._registry,
        )
        self._error_rate = Gauge(
            "sre_error_rate_percent",
            "Current error rate percentage",
            ["service_name"],
            registry=self._registry,
        )
        self._cpu = Gauge(
            "sre_cpu_percent",
            "CPU usage percentage",
            ["service_name"],
            registry=self._registry,
        )
        self._memory = Gauge(
            "sre_memory_percent",
            "Memory usage percentage",
            ["service_name"],
            registry=self._registry,
        )
        self._throughput = Gauge(
            "sre_throughput_rps",
            "Requests per second throughput",
            ["service_name"],
            registry=self._registry,
        )
        self._uptime = Gauge(
            "sre_uptime_percent",
            "Service uptime percentage",
            ["service_name"],
            registry=self._registry,
        )
        self._upload_time = Gauge(
            "sre_upload_time_ms",
            "Upload time in milliseconds",
            ["service_name"],
            registry=self._registry,
        )
        self._degrading_flag = Gauge(
            "sre_is_degrading",
            "1 if service is in degraded state",
            ["service_name"],
            registry=self._registry,
        )

        self.app = FastAPI(title=name)
        self._register_routes()

        # Start background thread to continuously simulate requests
        # hitting the histogram — this gives Prometheus enough data
        # points to compute accurate p50/p95/p99 percentiles
        import threading
        self._sim_thread = threading.Thread(
            target=self._simulate_requests,
            daemon=True,
            name=f"sim-{name}",
        )
        self._sim_thread.start()

    def _simulate_requests(self):
        """
        Background thread that continuously feeds the histogram
        with simulated request latencies at ~10 req/s.
        This is what makes p50/p95/p99 accurate in Prometheus.
        Without this, the histogram only gets one observation
        per 15s scrape which isn't enough for percentiles.
        """
        import time as _time
        while True:
            try:
                m = self._generate_metrics()
                lbl = {"service_name": self.name}
                # Simulate a small burst of requests
                for _ in range(random.randint(3, 8)):
                    noise = random.gauss(0, self.rt_noise * 0.5)
                    simulated = max(1.0, m["response_time_ms"] + noise)
                    self._request_duration.labels(**lbl).observe(simulated)
                    self._requests_total.labels(**lbl).inc()
            except Exception:
                pass
            _time.sleep(1)   # simulate ~5 req/s continuously

    # ── Route registration ────────────────────────────────────────────────────

    def _register_routes(self):

        @self.app.get("/health")
        def health():
            if self._degrading and self._intensity > 0.85:
                return {"status": "unhealthy", "service": self.name}
            return {"status": "healthy", "service": self.name}

        @self.app.get("/metrics")
        def metrics():
            """Prometheus scrape endpoint — returns text exposition format."""
            # Generate current simulated metrics
            m = self._generate_metrics()

            # Update gauges with current simulated values
            lbl = {"service_name": self.name}
            self._requests_total.labels(**lbl).inc()

            # Observe the SIMULATED response time, not the actual HTTP time
            self._request_duration.labels(**lbl).observe(m["response_time_ms"])

            self._error_rate.labels(**lbl).set(m["error_rate_pct"])
            self._cpu.labels(**lbl).set(m["cpu_pct"])
            self._memory.labels(**lbl).set(m["memory_pct"])
            self._throughput.labels(**lbl).set(m["throughput_rps"])
            self._uptime.labels(**lbl).set(m["uptime_pct"])
            self._upload_time.labels(**lbl).set(m["upload_time_ms"])
            self._degrading_flag.labels(**lbl).set(1 if self._degrading else 0)

            # Also record multiple observations per scrape to simulate
            # real request traffic hitting the histogram buckets.
            # This gives Prometheus enough data points to compute
            # accurate p50/p95/p99 from the simulated latency distribution.
            for _ in range(4):
                noise = random.gauss(0, self.rt_noise)
                simulated = max(0, m["response_time_ms"] + noise)
                self._request_duration.labels(**lbl).observe(simulated)

            self._request_count += 1

            return Response(
                content=generate_latest(self._registry),
                media_type=CONTENT_TYPE_LATEST,
            )

        @self.app.get("/metrics/json")
        def metrics_json():
            """Legacy JSON metrics — used by collector as fallback."""
            self._request_count += 1
            return self._generate_metrics()

        @self.app.post("/degrade")
        def degrade(config: DegradationConfig):
            self._degrading = True
            self._intensity = max(0.0, min(1.0, config.intensity))
            return {
                "status":    "degradation started",
                "intensity": self._intensity,
                "service":   self.name,
            }

        @self.app.post("/recover")
        def recover():
            self._degrading  = False
            self._intensity  = 0.0
            self._surge      = False
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
        load  = self._load_curve_multiplier()
        surge = self._surge_mult if self._surge else 1.0
        intens = self._intensity if self._degrading else 0.0

        if self._degrading:
            rt  = self._noisy(self.base_rt * (1 + intens * 12), self.rt_noise * (1 + intens * 3))
            er  = self._clamp(self._noisy(self.base_er * (1 + intens * 10), self.er_noise * 3), 0, 100)
            tp  = self._noisy(self.base_tp * max(0.05, 1 - intens * 0.85), self.tp_noise)
            cpu = self._clamp(self._noisy(self.base_cpu * (1 + intens * 0.8), 5), 0, 100)
            mem = self._clamp(self._noisy(self.base_mem * (1 + intens * 0.5), 4), 0, 100)
            uptime = self._clamp(100 - intens * 40, 0, 100)
        elif self._surge:
            rt  = self._noisy(self.base_rt * (1 + (surge - 1) * 0.4), self.rt_noise * 2)
            er  = self._clamp(self._noisy(self.base_er * (1 + (surge - 1) * 0.2), self.er_noise), 0, 100)
            tp  = self._noisy(self.base_tp * surge, self.tp_noise * surge * 0.3)
            cpu = self._clamp(self._noisy(self.base_cpu * (1 + (surge - 1) * 0.5), 5), 0, 100)
            mem = self._clamp(self._noisy(self.base_mem * (1 + (surge - 1) * 0.2), 3), 0, 100)
            uptime = 100.0
        else:
            rt  = self._noisy(self.base_rt,  self.rt_noise,  load)
            er  = self._clamp(self._noisy(self.base_er, self.er_noise, load * 0.3), 0, 100)
            tp  = self._noisy(self.base_tp,  self.tp_noise,  load)
            cpu = self._clamp(self._noisy(self.base_cpu, self.rt_noise * 0.1, load), 0, 100)
            mem = self._clamp(self._noisy(self.base_mem, 3), 0, 100)
            uptime = 100.0

        return {
            "service":          self.name,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
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