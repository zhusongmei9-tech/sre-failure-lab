from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, ProcessCollector


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        self.cpu_iterations = Counter(
            "lab_cpu_iterations_total", "Completed synthetic CPU iterations", registry=self.registry
        )
        self.cpu_capped = Counter(
            "lab_cpu_budget_exhausted_total",
            "CPU requests stopped by time budget",
            registry=self.registry,
        )
        self.cpu_rejected = Counter(
            "lab_cpu_rejected_total",
            "CPU requests rejected while worker busy",
            registry=self.registry,
        )
        self.fault = Gauge(
            "lab_fault_active", "Active application fault", ["kind"], registry=self.registry
        )
        self.fallback = Counter(
            "lab_fallback_total", "Successful source fallbacks", registry=self.registry
        )
        self.source = Counter(
            "lab_source_reads_total", "Reads from the simulated source", registry=self.registry
        )
        self.requests = Counter(
            "lab_http_requests_total",
            "Completed product requests",
            ["status_code"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "lab_http_request_duration_seconds",
            "Product request duration to response headers",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5),
            registry=self.registry,
        )
        self.cache = Counter(
            "lab_cache_requests_total",
            "Product request cache outcomes (not Redis commands)",
            ["result"],
            registry=self.registry,
        )
        for result in ("hit", "miss", "invalid", "error"):
            self.cache.labels(result)
        for status in (200, 404, 422, 429, 500, 503):
            self.requests.labels(str(status))

    def observe(self, status, duration, cache_result):
        self.requests.labels(str(status)).inc()
        self.duration.observe(duration)
        if cache_result != "not_applicable":
            self.cache.labels(cache_result).inc()
