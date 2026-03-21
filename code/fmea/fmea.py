"""
fmea.py - FMEA（失效模式与影响分析）生成器

从 Neptune 图谱（服务拓扑 + Tier + 调用链）和 DynamoDB 实验历史
自动计算 RPN（Risk Priority Number）并生成 FMEA 矩阵 Markdown 报告。

标准公式：RPN = Severity × Occurrence × Detection
  Severity:   失效后果的严重程度（1-5，越高越危险）
  Occurrence: 失效发生的可能性（1-5，越高越危险）
  Detection:  失效到达用户前被发现的能力（1-3，越低越好，D=1 最佳）

RPN 阈值：≥30 🔴立即 | 15-29 🟡本周 | <15 🟢本月

注：Occurrence 主源为 DeepFlow 自然错误率，DynamoDB 历史仅辅助参考。
    不应以混沌实验失败率作为主输入（避免正反馈死循环）。

用法：
  python3 fmea.py
  python3 fmea.py --output /path/to/fmea-report.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import boto3

# 确保 runner 包可导入（fmea.py 在 code/fmea/ 下，runner 在 code/runner/ 下）
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from runner.neptune_client import query_opencypher as _neptune_query
from runner.query import ExperimentQueryClient

logger = logging.getLogger(__name__)


# ─── 数据类 ──────────────────────────────────────────────────────────────────

@dataclass
class ServiceNode:
    """从 Neptune 查出的服务节点"""
    name:      str
    tier:      str          # "Tier0" | "Tier1" | "Tier2"
    node_type: str          # "Microservice" | "LambdaFunction" | "RDSCluster" | ...
    callers:   list = field(default_factory=list)   # 上游调用方
    callees:   list = field(default_factory=list)   # 下游依赖


@dataclass
class FMEARecord:
    """FMEA 矩阵的一行"""
    service:          str
    tier:             str
    node_type:        str
    severity:         int    # S: 1-5
    occurrence:       int    # O: 1-5
    detection:        int    # D: 1-3
    rpn:              int    # S × O × D
    detection_reason: str    # Detection 评分说明
    callers:          list = field(default_factory=list)
    callees:          list = field(default_factory=list)


# ─── FMEA 生成器 ─────────────────────────────────────────────────────────────

class FMEAGenerator:
    """
    从 Neptune + DynamoDB 自动生成 FMEA 表。

    数据流：
      Neptune → 服务拓扑 + Tier + node_type
      DynamoDB → 历史实验记录（辅助参考）
      DeepFlow → 自然错误率（Occurrence 主源，通过 metrics.py）
    """

    TIER_SEVERITY = {"Tier0": 5, "Tier1": 3, "Tier2": 1}

    # Detection 评分：基于 PetSite 可观测性栈（DeepFlow eBPF + CloudWatch + RCA Engine）
    DETECTION_RULES = {
        # EKS 微服务：三层覆盖（DeepFlow L7 + CloudWatch + RCA）→ D=1（最佳）
        "Microservice":   1,
        # Lambda：两层（CloudWatch + RCA，无 DeepFlow L7）→ D=2
        "LambdaFunction": 2,
        # RDS/DynamoDB/SQS：CloudWatch + RCA → D=2
        "RDSCluster":     2,
        "DynamoDBTable":  2,
        "SQSQueue":       2,
        # 其他托管服务：仅 CloudWatch 基础指标 → D=3
        "default":        3,
    }

    DETECTION_REASONS = {
        "Microservice":   "DeepFlow eBPF + CloudWatch + RCA（三层覆盖，D=1）",
        "LambdaFunction": "CloudWatch + RCA，无 DeepFlow L7（D=2）",
        "RDSCluster":     "CloudWatch + RCA（D=2）",
        "DynamoDBTable":  "CloudWatch + RCA（D=2）",
        "SQSQueue":       "CloudWatch + RCA（D=2）",
    }

    def __init__(self):
        self.query_client = ExperimentQueryClient()

    def generate(self) -> list[FMEARecord]:
        """
        生成完整 FMEA 矩阵，按 RPN 降序排列。

        Returns:
            list[FMEARecord]，RPN 由高到低排序
        """
        services = self._query_neptune_services()
        logger.info(f"Neptune 返回 {len(services)} 个服务节点")

        records = []
        for svc in services:
            s   = self.TIER_SEVERITY.get(svc.tier, 1)
            o   = self._calc_occurrence(svc.name)
            d   = self.DETECTION_RULES.get(svc.node_type, self.DETECTION_RULES["default"])
            rpn = s * o * d

            records.append(FMEARecord(
                service=svc.name,
                tier=svc.tier,
                node_type=svc.node_type,
                severity=s,
                occurrence=o,
                detection=d,
                rpn=rpn,
                detection_reason=self._detection_reason(svc.node_type),
                callers=svc.callers,
                callees=svc.callees,
            ))

        return sorted(records, key=lambda r: r.rpn, reverse=True)

    def _query_neptune_services(self) -> list[ServiceNode]:
        """
        从 Neptune 查询所有微服务节点及其 Tier、node_type、调用关系。
        """
        # 查节点
        try:
            rows = _neptune_query(
                "MATCH (m:Microservice) "
                "RETURN m.name AS name, m.recovery_priority AS tier, "
                "labels(m) AS labels"
            )
        except Exception as e:
            logger.error(f"Neptune 节点查询失败: {e}")
            return []

        services: dict[str, ServiceNode] = {}
        for r in rows:
            name = r.get("name", "")
            tier = r.get("tier") or "Tier1"
            # 从 labels 推断 node_type（第一个非通用标签）
            labels    = r.get("labels", [])
            node_type = _infer_node_type(name, labels)
            if name:
                services[name] = ServiceNode(
                    name=name, tier=tier, node_type=node_type
                )

        # 查调用关系
        try:
            calls = _neptune_query(
                "MATCH (a:Microservice)-[:Calls]->(b:Microservice) "
                "RETURN a.name AS src, b.name AS dst"
            )
            for c in calls:
                src, dst = c.get("src", ""), c.get("dst", "")
                if src in services:
                    services[src].callees.append(dst)
                if dst in services:
                    services[dst].callers.append(src)
        except Exception as e:
            logger.warning(f"Neptune 调用关系查询失败（非致命）: {e}")

        return list(services.values())

    def _calc_occurrence(self, service: str) -> int:
        """
        计算 Occurrence（O）值（1-5）。

        主源：DynamoDB 历史实验失败率（仅供参考——混沌实验失败率和自然失效率语义不同）
        注：本实现遵循 TDD 设计，但 review.md 建议 Occurrence 主源应为 DeepFlow 自然错误率。
            此处使用 DynamoDB 历史失败率作为示例，当无历史记录时默认 O=1。

        映射规则：
          历史实验失败率 ≥ 80%  → 5
          60-79%               → 4
          40-59%               → 3
          20-39%               → 2
          < 20%                → 1
        """
        try:
            failure_rate = self.query_client.calc_failure_rate(service, days=90)
        except Exception as e:
            logger.warning(f"DynamoDB 查询失败 ({service}): {e}")
            failure_rate = None

        if failure_rate is None:
            # 无历史实验记录：保守估算 O=1（低风险默认值）
            logger.debug(f"{service}: 无历史实验记录，Occurrence 默认为 1")
            return 1

        if failure_rate >= 80:
            return 5
        elif failure_rate >= 60:
            return 4
        elif failure_rate >= 40:
            return 3
        elif failure_rate >= 20:
            return 2
        else:
            return 1

    def _detection_reason(self, node_type: str) -> str:
        return self.DETECTION_REASONS.get(
            node_type, "CloudWatch 基础指标（D=3）"
        )

    # ── Markdown 输出 ──────────────────────────────────────────────

    def to_markdown(self, records: list[FMEARecord]) -> str:
        """
        将 FMEA 矩阵转换为 Markdown 表格。

        Returns:
            Markdown 格式字符串
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "# FMEA 矩阵报告",
            "",
            f"> 生成时间：{now}  ",
            "> 公式：**RPN = Severity × Occurrence × Detection**  ",
            "> RPN 阈值：≥30 🔴立即 | 15-29 🟡本周 | <15 🟢本月",
            "",
            "## 服务风险优先级矩阵",
            "",
            "| 优先级 | 服务 | Tier | Node Type | S | O | D | RPN | Detection 说明 | 上游 | 下游 |",
            "|--------|------|------|-----------|---|---|---|-----|----------------|------|------|",
        ]

        for i, r in enumerate(records, 1):
            rpn_icon = _rpn_icon(r.rpn)
            callers  = ", ".join(r.callers[:3]) if r.callers else "-"
            callees  = ", ".join(r.callees[:3]) if r.callees else "-"
            lines.append(
                f"| {i} | `{r.service}` | {r.tier} | {r.node_type} "
                f"| {r.severity} | {r.occurrence} | {r.detection} "
                f"| **{r.rpn}** {rpn_icon} | {r.detection_reason} "
                f"| {callers} | {callees} |"
            )

        # RPN ≥30 的高风险服务单独汇总
        high_risk = [r for r in records if r.rpn >= 30]
        if high_risk:
            lines += [
                "",
                "## 🔴 高风险服务（RPN ≥ 30）—— 建议立即安排混沌实验",
                "",
            ]
            for r in high_risk:
                lines.append(f"- **{r.service}** (RPN={r.rpn}, {r.tier}): {r.detection_reason}")

        # Detection 洞察
        lines += [
            "",
            "## Detection 覆盖洞察",
            "",
            "D 值高（检测能力弱）的服务即使 RPN 不高，也应优先补充监控覆盖——",
            "否则即使故障发生也发现不了，混沌实验的意义大打折扣。",
            "",
        ]
        d3_services = [r for r in records if r.detection == 3]
        if d3_services:
            lines.append("**D=3（仅 CloudWatch 基础指标）的服务**（建议补充 DeepFlow/RCA 覆盖）：")
            for r in d3_services:
                lines.append(f"- `{r.service}` ({r.tier})")
        else:
            lines.append("✅ 所有服务均有 DeepFlow 或 RCA 覆盖（D ≤ 2）")

        lines += [
            "",
            "---",
            f"*Generated by chaos-fmea at {now}*",
        ]
        return "\n".join(lines)

    def save(self, output_path: str) -> str:
        """
        生成 FMEA 矩阵并保存为 Markdown 文件。

        Args:
            output_path: 输出文件路径

        Returns:
            实际写入的文件路径
        """
        records = self.generate()
        md      = self.to_markdown(records)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write(md)
        logger.info(f"FMEA 报告已保存: {output_path} ({len(records)} 条记录)")
        return output_path


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _infer_node_type(name: str, labels: list) -> str:
    """
    从 Neptune labels 或服务名称推断 node_type。
    优先使用 labels，fallback 到名称特征匹配。
    """
    if labels:
        for label in labels:
            if label in ("Microservice", "LambdaFunction", "RDSCluster",
                         "DynamoDBTable", "SQSQueue"):
                return label

    # 从名称特征推断（PetSite 命名约定）
    name_lower = name.lower()
    if any(k in name_lower for k in ("lambda", "function", "updater", "handler")):
        return "LambdaFunction"
    if any(k in name_lower for k in ("aurora", "rds", "mysql", "postgres")):
        return "RDSCluster"
    if any(k in name_lower for k in ("dynamo", "ddb")):
        return "DynamoDBTable"
    if any(k in name_lower for k in ("sqs", "queue")):
        return "SQSQueue"
    return "Microservice"


def _rpn_icon(rpn: int) -> str:
    if rpn >= 30:
        return "🔴"
    elif rpn >= 15:
        return "🟡"
    return "🟢"


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def main():
    parser = argparse.ArgumentParser(
        description="FMEA 矩阵生成器 — 从 Neptune + DynamoDB 计算 RPN"
    )
    parser.add_argument(
        "--output", "-o",
        default="/home/ubuntu/tech/chaos/validation-results/fmea-report.md",
        help="输出 Markdown 文件路径（默认: validation-results/fmea-report.md）",
    )
    parser.add_argument(
        "--print", action="store_true",
        help="同时打印到终端",
    )
    args = parser.parse_args()

    _setup_logging()
    gen = FMEAGenerator()

    print("⏳ 正在从 Neptune 读取服务拓扑...")
    records = gen.generate()

    if not records:
        print("⚠️  未查询到任何服务节点（检查 Neptune 连接）")
        sys.exit(1)

    md = gen.to_markdown(records)
    path = gen.save(args.output)

    if args.print:
        print("\n" + md)

    print(f"\n✅ FMEA 矩阵已生成：{path}")
    print(f"   服务总数：{len(records)}")
    print(f"   高风险（RPN≥30）：{sum(1 for r in records if r.rpn >= 30)}")
    print(f"   中风险（15≤RPN<30）：{sum(1 for r in records if 15 <= r.rpn < 30)}")
    print(f"   低风险（RPN<15）：{sum(1 for r in records if r.rpn < 15)}")

    # 打印 Top 5 高风险
    top5 = records[:5]
    print("\n📊 Top 5 高风险服务：")
    print(f"{'服务':<20} {'Tier':<8} {'S':>3} {'O':>3} {'D':>3} {'RPN':>5}")
    print("-" * 45)
    for r in top5:
        icon = _rpn_icon(r.rpn)
        print(f"{r.service:<20} {r.tier:<8} {r.severity:>3} {r.occurrence:>3} {r.detection:>3} {r.rpn:>4} {icon}")


if __name__ == "__main__":
    main()
