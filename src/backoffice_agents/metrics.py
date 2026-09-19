"""Metricas operacionais da fila, calculadas do banco, com exportacao Prometheus e CloudWatch.

Sem emojis neste modulo: a saida pode ir para coletores que nao lidam bem com Unicode.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .costs import cost_report
from .storage import Store

TERMINAL = {"sent", "escalated", "discarded", "failed"}


def _p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=20)[-1]


def _age_minutes(iso: str, now: datetime) -> float:
    try:
        return max(0.0, (now - datetime.fromisoformat(iso)).total_seconds() / 60)
    except ValueError:
        return 0.0


def collect_metrics(store: Store, settings: Settings, window_min: int = 60) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = (now - timedelta(minutes=window_min)).isoformat(timespec="seconds")
    items = store.list_items()
    by_status = Counter(i["status"] for i in items)
    recent = [i for i in items if i["updated_at"] >= since]
    processed = sum(1 for i in recent if i["status"] in TERMINAL)
    errors = sum(1 for i in recent if i["status"] in {"error", "failed"})
    escalated = sum(1 for i in recent if i["status"] == "escalated")

    pending = store.list_approvals("pending")
    ages = [_age_minutes(a["requested_at"], now) for a in pending]

    calls = [c for c in store.list_model_calls() if c["created_at"] >= since]
    latency: dict[str, dict[str, float]] = {}
    for kind in ("llm", "jev"):
        values = [float(c["latency_ms"]) for c in calls if c["kind"] == kind]
        latency[kind] = {"count": len(values), "mean_ms": statistics.fmean(values) if values else 0.0,
                         "p95_ms": _p95(values)}
    cost = cost_report(calls, settings)

    return {
        "window_min": window_min,
        "queue_depth": by_status.get("new", 0) + by_status.get("error", 0),
        "by_status": dict(by_status),
        "processed": processed,
        "errors": errors,
        "error_rate": errors / processed if processed else 0.0,
        "escalated": escalated,
        "escalation_rate": escalated / processed if processed else 0.0,
        "pending_approvals": len(pending),
        "oldest_pending_approval_min": max(ages) if ages else 0.0,
        "latency": latency,
        "cost_usd": cost.total_cost_usd,
        "cost_per_item_usd": cost.cost_per_item_usd,
        "collected_at": now.isoformat(timespec="seconds"),
    }


def to_prometheus(metrics: dict[str, Any], prefix: str = "backoffice") -> str:
    lines = [
        f"# HELP {prefix}_queue_depth itens aguardando processamento (new + error)",
        f"# TYPE {prefix}_queue_depth gauge",
        f"{prefix}_queue_depth {metrics['queue_depth']}",
        f"# TYPE {prefix}_items_by_status gauge",
    ]
    lines += [f'{prefix}_items_by_status{{status="{s}"}} {n}'
              for s, n in sorted(metrics["by_status"].items())]
    lines += [
        f"# TYPE {prefix}_processed_window gauge",
        f"{prefix}_processed_window {metrics['processed']}",
        f"# TYPE {prefix}_error_rate gauge",
        f"{prefix}_error_rate {metrics['error_rate']:.4f}",
        f"# TYPE {prefix}_escalation_rate gauge",
        f"{prefix}_escalation_rate {metrics['escalation_rate']:.4f}",
        f"# TYPE {prefix}_pending_approvals gauge",
        f"{prefix}_pending_approvals {metrics['pending_approvals']}",
        f"# TYPE {prefix}_oldest_pending_approval_minutes gauge",
        f"{prefix}_oldest_pending_approval_minutes {metrics['oldest_pending_approval_min']:.1f}",
        f"# TYPE {prefix}_model_latency_ms gauge",
    ]
    for kind, lat in metrics["latency"].items():
        lines.append(f'{prefix}_model_latency_ms{{kind="{kind}",stat="mean"}} {lat["mean_ms"]:.1f}')
        lines.append(f'{prefix}_model_latency_ms{{kind="{kind}",stat="p95"}} {lat["p95_ms"]:.1f}')
        lines.append(f'{prefix}_model_calls_window{{kind="{kind}"}} {lat["count"]}')
    lines += [f"# TYPE {prefix}_cost_usd_window gauge", f"{prefix}_cost_usd_window {metrics['cost_usd']:.6f}"]
    return "\n".join(lines) + "\n"


def cloudwatch_metric_data(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    data = [
        {"MetricName": "QueueDepth", "Value": metrics["queue_depth"], "Unit": "Count"},
        {"MetricName": "ProcessedWindow", "Value": metrics["processed"], "Unit": "Count"},
        {"MetricName": "ErrorRate", "Value": metrics["error_rate"], "Unit": "None"},
        {"MetricName": "EscalationRate", "Value": metrics["escalation_rate"], "Unit": "None"},
        {"MetricName": "PendingApprovals", "Value": metrics["pending_approvals"], "Unit": "Count"},
        {"MetricName": "OldestPendingApprovalMinutes", "Value": metrics["oldest_pending_approval_min"],
         "Unit": "None"},
        {"MetricName": "CostUsdWindow", "Value": metrics["cost_usd"], "Unit": "None"},
    ]
    for kind, lat in metrics["latency"].items():
        data.append({"MetricName": "ModelLatencyP95", "Value": lat["p95_ms"], "Unit": "Milliseconds",
                     "Dimensions": [{"Name": "Kind", "Value": kind}]})
    return data


def push_cloudwatch(metrics: dict[str, Any], namespace: str, region: str | None = None) -> int:
    """Envia as metricas ao CloudWatch. Exige o extra 'aws' (boto3) e credenciais configuradas."""
    import boto3  # importado aqui para o resto do sistema nao depender de boto3

    client = boto3.client("cloudwatch", region_name=region)
    data = cloudwatch_metric_data(metrics)
    client.put_metric_data(Namespace=namespace, MetricData=data)
    return len(data)


def alerts(metrics: dict[str, Any], settings: Settings) -> list[str]:
    """Condicoes que merecem aviso ao operador."""
    found = []
    oldest = metrics["oldest_pending_approval_min"]
    if metrics["pending_approvals"] and oldest > settings.alert_approval_max_age_min:
        found.append(f"aprovacao pendente ha {metrics['oldest_pending_approval_min']:.0f} min "
                     f"(limite {settings.alert_approval_max_age_min})")
    if metrics["queue_depth"] > settings.alert_queue_depth:
        found.append(f"fila com {metrics['queue_depth']} itens (limite {settings.alert_queue_depth})")
    if metrics["processed"] >= 5 and metrics["error_rate"] > settings.alert_error_rate:
        found.append(f"taxa de erro {metrics['error_rate']:.0%} na ultima hora "
                     f"(limite {settings.alert_error_rate:.0%})")
    return found
