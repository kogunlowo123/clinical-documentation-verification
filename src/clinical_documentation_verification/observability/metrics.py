"""Service metrics exposed on /metrics in Prometheus text format (own registry)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class VerificationMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "cdv_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self.verifications = Counter(
            "cdv_verifications_total",
            "Verifications completed, by consistency band and extraction source",
            ["band", "source"],
            registry=self.registry,
        )
        self.consistency = Histogram(
            "cdv_consistency_score",
            "Consistency score of completed verifications",
            registry=self.registry,
            buckets=(0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0),
        )
        self.findings = Counter(
            "cdv_findings_total",
            "Findings raised, by rule and severity",
            ["rule", "severity"],
            registry=self.registry,
        )
        self.chart_diff = Counter(
            "cdv_chart_diff_total",
            "Chart reconciliation entries, by category and kind",
            ["category", "kind"],
            registry=self.registry,
        )
        self.extraction_attempts = Counter(
            "cdv_model_extraction_attempts_total",
            "Model extraction runs by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.decisions = Counter(
            "cdv_clinician_decisions_total",
            "Clinician sign-off decisions",
            ["decision", "override"],
            registry=self.registry,
        )
        self.fhir_writes = Counter(
            "cdv_fhir_writes_total",
            "Chart write attempts by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "cdv_mcp_tool_calls_total",
            "MCP tool calls by tool and outcome",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.a2a = Counter(
            "cdv_a2a_requests_total",
            "A2A JSON-RPC requests by method and outcome",
            ["method", "outcome"],
            registry=self.registry,
        )
