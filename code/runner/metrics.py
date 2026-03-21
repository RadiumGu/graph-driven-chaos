"""
metrics.py - DeepFlow 指标采集（ClickHouse 8123 直连）

表：flow_log.l7_flow_log
  response_status: 0=正常, >=1=异常（1=异常,2=不存在,3=服务端异常,4=客户端异常）
  response_duration: 微秒
  request_domain: 格式 svc-name.namespace.svc.cluster.local
"""
from __future__ import annotations
import time
import logging
import requests
from dataclasses import dataclass

from .experiment import MetricsSnapshot
from .config import DEEPFLOW_CH_HOST, DEEPFLOW_CH_PORT

logger = logging.getLogger(__name__)


def _ch_query(sql: str) -> dict:
    """执行 ClickHouse 查询，返回 FORMAT JSON 结果"""
    try:
        r = requests.post(
            f"http://{DEEPFLOW_CH_HOST}:{DEEPFLOW_CH_PORT}/",
            data=(sql + " FORMAT JSON").encode(),
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"ClickHouse 查询失败: {e}")
        return {}


class DeepFlowMetrics:
    """
    查询 DeepFlow ClickHouse，获取服务实时 SLI 指标
    通过 request_domain 字段匹配服务（含 K8s DNS 格式）
    """

    def collect(
        self,
        service: str,
        namespace: str = "default",
        window_seconds: int = 60,
    ) -> MetricsSnapshot:
        """
        查询指定服务近 window_seconds 秒的指标
        服务名匹配 request_domain LIKE '%{service}%'
        """
        sql = f"""
SELECT
    countIf(response_status = 0) AS success_cnt,
    count() AS total_cnt,
    quantile(0.99)(response_duration) / 1000.0 AS p99_latency_ms
FROM flow_log.l7_flow_log
WHERE start_time > now() - INTERVAL {window_seconds} SECOND
  AND response_duration > 0
  AND request_domain LIKE '%{service}%'
"""
        ts = int(time.time())
        try:
            data = _ch_query(sql)
            rows = data.get("data", [])
            if rows:
                row = rows[0]
                total   = int(row.get("total_cnt", 0) or 0)
                success = int(row.get("success_cnt", 0) or 0)
                p99     = float(row.get("p99_latency_ms", 0) or 0)
                success_rate = round(success / total * 100, 2) if total > 0 else 100.0
                return MetricsSnapshot(
                    timestamp=ts,
                    success_rate=success_rate,
                    latency_p99_ms=round(p99, 1),
                    total_requests=total,
                )
        except Exception as e:
            logger.warning(f"metrics.collect({service}) 失败: {e}")

        # fallback: 无数据时返回 100% 成功（表示流量为零或查询失败，不触发 guardrail）
        return MetricsSnapshot(timestamp=ts, success_rate=100.0, latency_p99_ms=0.0, total_requests=0)

    def collect_steady(
        self,
        service: str,
        namespace: str = "default",
        window_seconds: int = 60,
        samples: int = 3,
        interval: int = 10,
    ) -> MetricsSnapshot:
        """
        采集多次取平均，用于稳态基线（Phase 1）和恢复验证（Phase 5）
        """
        snapshots = []
        for i in range(samples):
            snap = self.collect(service, namespace, window_seconds)
            snapshots.append(snap)
            if i < samples - 1:
                time.sleep(interval)

        ts = int(time.time())
        avg_sr  = round(sum(s.success_rate for s in snapshots) / len(snapshots), 2)
        avg_p99 = round(sum(s.latency_p99_ms for s in snapshots) / len(snapshots), 1)
        total   = snapshots[-1].total_requests
        logger.info(f"稳态快照 {service}: success_rate={avg_sr}%, p99={avg_p99}ms (n={samples})")
        return MetricsSnapshot(timestamp=ts, success_rate=avg_sr, latency_p99_ms=avg_p99, total_requests=total)
