"""
graph_feedback.py - 实验结果写回 Neptune 图谱

通过统一 neptune_client.py（SigV4 直连）写回 Gremlin 查询。
启动前执行 connectivity check，不可用时记录警告而非静默失败。
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .result import ExperimentResult

from .neptune_client import query_gremlin, check_connectivity

logger = logging.getLogger(__name__)

# 需要写回的故障类型（对应 Calls 边）
CALLS_EDGE_FAULT_TYPES = {
    "pod_kill", "pod_failure", "pod_failure",
    "network_delay", "network_loss", "network_corrupt",
    "network_partition", "http_chaos",
}


class GraphFeedback:
    """
    将混沌实验结果写回 Neptune，更新 Calls 边和 Microservice 节点属性
    """

    def write_back(self, result: "ExperimentResult"):
        if result.status not in ("PASSED", "FAILED", "ABORTED"):
            logger.info(f"跳过 Neptune 写回（status={result.status}）")
            return

        # 写回前检查 Neptune 连通性，不可用则提前失败（有明确日志）
        if not check_connectivity():
            raise RuntimeError("Neptune 不可达，图谱反馈跳过（检查 VPC 网络 / IAM 权限）")

        degradation = result.degradation_rate()
        dep_type    = self._classify(degradation)
        last_verified = result.end_time.isoformat() if result.end_time else ""
        exp_id = result.experiment_id

        props = {
            "chaos_dependency_type":       dep_type,
            "chaos_degradation_rate":      round(degradation, 2),
            "chaos_recovery_time_seconds": round(result.recovery_seconds or 0, 1),
            "chaos_last_verified":         last_verified,
            "chaos_verified_by":           exp_id,
        }

        svc = result.experiment.target_service

        # 1. 更新 Calls 边（pod/network/http chaos）
        if result.experiment.fault.type in CALLS_EDGE_FAULT_TYPES:
            self._update_calls_edges(svc, props)

        # 2. 更新 Microservice 节点弹性属性
        self._update_node(svc, result, dep_type)

        # 3. dependency_type=none → 可疑边告警
        if dep_type == "none":
            logger.warning(
                f"⚠️ SUSPICIOUS EDGE: {svc} 的 Calls 边 "
                f"degradation_rate={degradation:.1f}% → 依赖可能是 ETL 误识别，"
                f"建议人工复核 Neptune 图谱。experiment_id={exp_id}"
            )

    def _update_calls_edges(self, svc: str, props: dict):
        """更新与目标服务相关的所有 Calls 边"""
        gremlin = f"""
g.E().hasLabel('Calls')
 .where(__.outV().has('name', '{svc}')
   .or_(__.inV().has('name', '{svc}')))
 .property(single, 'chaos_dependency_type',       '{props["chaos_dependency_type"]}')
 .property(single, 'chaos_degradation_rate',      {props["chaos_degradation_rate"]})
 .property(single, 'chaos_recovery_time_seconds', {props["chaos_recovery_time_seconds"]})
 .property(single, 'chaos_last_verified',         '{props["chaos_last_verified"]}')
 .property(single, 'chaos_verified_by',           '{props["chaos_verified_by"]}')
""".strip()
        try:
            self._run_gremlin(gremlin)
            logger.info(f"✅ Neptune Calls 边已更新: {svc} → {props['chaos_dependency_type']}")
        except Exception as e:
            logger.error(f"Neptune Calls 边更新失败: {e}")

    def _update_node(self, svc: str, result: "ExperimentResult", dep_type: str):
        """更新 Microservice 节点弹性属性"""
        score        = self._calc_resilience_score(result, dep_type)
        last_tested  = result.end_time.isoformat() if result.end_time else ""

        gremlin = f"""
g.V().hasLabel('Microservice').has('name', '{svc}')
 .property(single, 'last_chaos_test',   '{last_tested}')
 .property(single, 'resilience_score',  {score})
 .sideEffect(
     __.coalesce(
         __.values('chaos_test_count').store('x'),
         __.constant(0).store('x')
     )
 )
 .property(single, 'chaos_test_count',
     __.cap('x').unfold().math('_ + 1'))
""".strip()
        try:
            self._run_gremlin(gremlin)
            logger.info(f"✅ Neptune 节点已更新: {svc} resilience_score={score}")
        except Exception as e:
            logger.warning(f"Neptune 节点更新失败（非致命）: {e}")

    def _calc_resilience_score(self, result: "ExperimentResult", dep_type: str) -> int:
        score = max(0, int(100 - result.degradation_rate()))
        if result.recovery_seconds is not None:
            if result.recovery_seconds < 60:
                score = min(100, score + 5)
            elif result.recovery_seconds > 300:
                score = max(0, score - 10)
        if result.status == "ABORTED":
            score = max(0, score - 10)
        return score

    def _classify(self, degradation_rate: float) -> str:
        if degradation_rate >= 80:   return "strong"
        elif degradation_rate >= 20: return "weak"
        else:                        return "none"

    def _run_gremlin(self, query: str):
        """
        执行 Gremlin 查询，通过 neptune_client.py SigV4 直连 Neptune。
        """
        query_gremlin(query)
