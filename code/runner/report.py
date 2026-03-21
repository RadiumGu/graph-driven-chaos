"""
report.py - Markdown 报告生成 + DynamoDB 写入 + LLM 分析结论
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from .result import ExperimentResult

logger = logging.getLogger(__name__)

from .config import REGION, BEDROCK_REGION, BEDROCK_MODEL, DYNAMODB_TABLE, CHAOS_RECORD_TTL_SECONDS

TABLE_NAME = DYNAMODB_TABLE
REPORT_DIR = "/home/ubuntu/tech/chaos/validation-results"


class Reporter:

    def __init__(self):
        self._ddb = None

    @property
    def ddb(self):
        if self._ddb is None:
            self._ddb = boto3.client("dynamodb", region_name=REGION)
        return self._ddb

    # ── Markdown 报告 ─────────────────────────────────────────────────────────

    def _generate_llm_analysis(self, result: "ExperimentResult") -> str:
        """
        调用 Bedrock Claude 生成实验分析结论。
        失败时静默返回空（不阻断报告生成）。
        """
        # 跳过无意义的分析（实验没跑起来）
        if result.status in ("ERROR",) and not result.snapshots:
            return ""
        if result.status == "ABORTED" and not result.inject_time:
            return ""

        try:
            from botocore.config import Config as _BotocoreConfig
            bedrock = boto3.client(
                "bedrock-runtime",
                region_name=BEDROCK_REGION,
                config=_BotocoreConfig(retries={"mode": "adaptive", "max_attempts": 5}),
            )
        except Exception as e:
            logger.warning(f"Bedrock 客户端初始化失败，跳过 LLM 分析: {e}")
            return ""

        exp = result.experiment
        ssb = result.steady_state_before
        ssa = result.steady_state_after

        # 构建实验数据摘要给 LLM
        data_summary = {
            "experiment_name": exp.name,
            "backend": exp.backend,
            "target_service": exp.target_service,
            "target_tier": exp.target_tier,
            "fault_type": exp.fault.type,
            "fault_mode": f"{exp.fault.mode} {exp.fault.value}",
            "fault_duration": exp.fault.duration,
            "status": result.status,
            "abort_reason": result.abort_reason or None,
            "duration_seconds": round(result.duration_seconds, 1),
            "steady_state_before": {
                "success_rate": round(ssb.success_rate, 1) if ssb else None,
                "latency_p99_ms": round(ssb.latency_p99_ms, 0) if ssb else None,
            },
            "steady_state_after": {
                "success_rate": round(ssa.success_rate, 1) if ssa else None,
                "latency_p99_ms": round(ssa.latency_p99_ms, 0) if ssa else None,
            },
            "impact_min_success_rate": round(result.min_success_rate, 1),
            "impact_max_latency_p99": round(result.max_latency_p99, 0),
            "recovery_seconds": round(result.recovery_seconds, 1) if result.recovery_seconds else None,
            "observation_points": len(result.snapshots),
        }

        # RCA 信息
        if result.rca_result and result.rca_result.status == "success":
            data_summary["rca"] = {
                "root_cause": result.rca_result.root_cause,
                "confidence": result.rca_result.confidence,
                "expected": exp.rca.expected_root_cause,
                "match": result.rca_match,
            }

        prompt = f"""你是一位资深 SRE，正在审阅混沌工程实验报告。请基于以下实验数据，给出简洁的分析结论。

实验数据：
```json
{json.dumps(data_summary, ensure_ascii=False, indent=2)}
```

要求：
1. 用 2-4 段话总结实验发现，不要重复列数据
2. 判断系统弹性是否达标，说明理由
3. 如果有异常或需要关注的点，明确指出
4. 如果实验失败或被熔断，分析可能原因和建议的下一步
5. 语气专业直接，面向 SRE / 架构师读者
6. 用中文输出"""

        try:
            resp = bedrock.invoke_model(
                modelId=BEDROCK_MODEL,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode(),
            )
            body = json.loads(resp["body"].read())
            analysis = body.get("content", [{}])[0].get("text", "")
            logger.info(f"✅ LLM 分析生成完成 ({len(analysis)} chars)")
            return analysis.strip()
        except Exception as e:
            logger.warning(f"LLM 分析生成失败（非致命）: {e}")
            return ""

    def generate_markdown(self, result: "ExperimentResult") -> str:
        exp  = result.experiment
        icon = {"PASSED": "✅", "FAILED": "❌", "ABORTED": "🛑", "ERROR": "💥"}.get(result.status, "❓")

        # 稳态快照
        ssb = result.steady_state_before
        ssa = result.steady_state_after

        # 观测窗口指标
        min_sr  = result.min_success_rate
        max_p99 = result.max_latency_p99

        lines = [
            f"# {icon} Chaos Experiment Report",
            f"",
            f"| 字段 | 值 |",
            f"|------|-----|",
            f"| 实验 ID | `{result.experiment_id}` |",
            f"| 实验名 | {exp.name} |",
            f"| 后端 | `{exp.backend}` |",
            f"| 目标服务 | `{exp.target_service}` ({exp.target_tier}) |",
            f"| 故障类型 | `{exp.fault.type}` ({exp.fault.mode} {exp.fault.value}) |",
            f"| 故障时长 | {exp.fault.duration} |",
            f"| 状态 | **{result.status}** |",
            f"| 开始时间 | {result.start_time.strftime('%Y-%m-%d %H:%M:%S %Z') if result.start_time else '-'} |",
            f"| 结束时间 | {result.end_time.strftime('%Y-%m-%d %H:%M:%S %Z') if result.end_time else '-'} |",
            f"| 总耗时 | {result.duration_seconds:.0f}s |",
        ]

        if result.abort_reason:
            lines += [f"| 熔断原因 | ⚠️ {result.abort_reason} |"]

        lines += ["", "## 稳态快照", "", "| 阶段 | success_rate | latency_p99 |", "|------|-------------|-------------|"]
        if ssb:
            lines.append(f"| 注入前 | {ssb.success_rate:.1f}% | {ssb.latency_p99_ms:.0f}ms |")
        if ssa:
            lines.append(f"| 恢复后 | {ssa.success_rate:.1f}% | {ssa.latency_p99_ms:.0f}ms |")

        lines += [
            "",
            "## 实验期间影响",
            "",
            f"- 成功率最低值：**{min_sr:.1f}%**",
            f"- P99 延迟最高值：**{max_p99:.0f}ms**",
            f"- 恢复耗时：{result.recovery_seconds:.0f}s" if result.recovery_seconds else "- 恢复耗时：-",
        ]

        # 观测快照时序
        if result.snapshots:
            lines += ["", "## 观测时序（每 10s）", "", "| T+秒 | success_rate | p99_ms |", "|------|-------------|--------|"]
            t0 = result.snapshots[0].timestamp
            for snap in result.snapshots:
                dt = snap.timestamp - t0
                lines.append(f"| {dt:>4}s | {snap.success_rate:.1f}% | {snap.latency_p99_ms:.0f} |")

        # RCA
        lines += ["", "## RCA 分析"]
        rca = result.rca_result
        if rca is None or rca.status == "not_triggered":
            # 区分原因：是配置关闭还是实验没跑到 Phase 3
            if not result.experiment.rca.enabled:
                lines.append("\n> ⏭️ RCA 未启用（`rca.enabled: false`）")
            elif result.status in ("ABORTED", "ERROR") and not result.inject_time:
                lines.append("\n> ⏭️ RCA 未触发（实验在 Phase 2 前终止，未进入观测阶段）")
            elif result.status in ("ABORTED", "ERROR"):
                lines.append("\n> ⏭️ RCA 未触发（实验在 Phase 3 提前终止，未达到触发延时）")
            else:
                lines.append("\n> ⏭️ RCA 未触发")
        elif rca.status == "error":
            lines += [
                f"",
                f"- 状态：**⚠️ 触发但失败**",
                f"- 错误：`{rca.error_message}`",
            ]
            if rca.raw:
                lines.append(f"- Lambda 原始响应（截断）：`{str(rca.raw)[:200]}`")
        elif rca.status == "success":
            match_icon = "✅" if result.rca_match else "❌"
            lines += [
                f"",
                f"- 根因：**{rca.root_cause}**",
                f"- 置信度：{rca.confidence:.0%}",
                f"- 期望：{result.experiment.rca.expected_root_cause or '-'}",
                f"- 命中：{match_icon}",
            ]

        # 稳态验证结果
        lines += ["", "## 稳态验证（Phase 5）"]
        for check_result in result.steady_state_after_checks:
            icon2 = "✅" if check_result["passed"] else "❌"
            lines.append(f"- {icon2} {check_result['desc']}")

        # FIS 特有信息
        if exp.backend == "fis":
            lines += ["", "## FIS 实验信息"]
            if result.fis_template_id:
                lines.append(f"- FIS Template ID: `{result.fis_template_id}`")
            lines.append(f"- FIS Experiment ID: `{result.chaos_experiment_name}`")
            # 列出 CloudWatch Alarm Stop Conditions
            cw_alarms = [sc.cloudwatch_alarm_arn for sc in exp.stop_conditions if sc.cloudwatch_alarm_arn]
            if cw_alarms:
                lines.append(f"- CloudWatch Alarm Stop Conditions:")
                for arn in cw_alarms:
                    lines.append(f"  - `{arn}`")

        # LLM 分析结论
        llm_analysis = self._generate_llm_analysis(result)
        if llm_analysis:
            lines += ["", "## 🧠 AI 分析结论", "", llm_analysis]

        lines += ["", "---", f"*Generated by chaos-runner at {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}*"]
        return "\n".join(lines)

    def save_report(self, result: "ExperimentResult") -> str:
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"{result.experiment_id}-report.md")
        content = self.generate_markdown(result)
        with open(path, "w") as f:
            f.write(content)
        result.report_path = path
        logger.info(f"📄 报告已保存: {path}")
        return path

    # ── DynamoDB ─────────────────────────────────────────────────────────────

    def save_to_dynamodb(self, result: "ExperimentResult"):
        exp = result.experiment
        ssb = result.steady_state_before
        ssa = result.steady_state_after

        item: dict = {
            "experiment_id":            {"S": result.experiment_id},
            "start_time":               {"S": result.start_time.isoformat() if result.start_time else ""},
            "experiment_name":          {"S": exp.name},
            "target_service":           {"S": exp.target_service},
            "target_namespace":         {"S": exp.target_namespace},
            "target_tier":              {"S": exp.target_tier},
            "fault_type":               {"S": exp.fault.type},
            "fault_mode":               {"S": exp.fault.mode},
            "fault_value":              {"S": exp.fault.value},
            "fault_duration":           {"S": exp.fault.duration},
            "backend":                  {"S": exp.backend},
            "status":                   {"S": result.status},
            "end_time":                 {"S": result.end_time.isoformat() if result.end_time else ""},
            "duration_seconds":         {"N": str(round(result.duration_seconds, 1))},
            "impact_min_success_rate":  {"N": str(round(result.min_success_rate, 2))},
            "impact_max_latency_p99":   {"N": str(round(result.max_latency_p99, 1))},
            "rca_enabled":              {"BOOL": exp.rca.enabled},
            "report_path":              {"S": result.report_path or ""},
            "yaml_source":              {"S": exp.yaml_source},
            "ttl":                      {"N": str(int(time.time()) + CHAOS_RECORD_TTL_SECONDS)},
        }

        if ssb:
            item["steady_state_before"] = {"M": {
                "success_rate":   {"N": str(round(ssb.success_rate, 2))},
                "latency_p99_ms": {"N": str(round(ssb.latency_p99_ms, 1))},
            }}
        if ssa:
            item["steady_state_after"] = {"M": {
                "success_rate":   {"N": str(round(ssa.success_rate, 2))},
                "latency_p99_ms": {"N": str(round(ssa.latency_p99_ms, 1))},
            }}

        if result.recovery_seconds is not None:
            item["recovery_seconds"] = {"N": str(round(result.recovery_seconds, 1))}
        if result.abort_reason:
            item["abort_reason"] = {"S": result.abort_reason}
        if exp.rca.expected_root_cause:
            item["rca_expected"] = {"S": exp.rca.expected_root_cause}
        if result.rca_result:
            item["rca_status"] = {"S": result.rca_result.status}
            if result.rca_result.error_message:
                item["rca_error"] = {"S": result.rca_result.error_message[:500]}
            if result.rca_result.root_cause:
                item["rca_actual"]     = {"S": result.rca_result.root_cause}
                item["rca_confidence"] = {"N": str(round(result.rca_result.confidence, 3))}
                item["rca_match"]      = {"BOOL": bool(result.rca_match)}

        try:
            self.ddb.put_item(TableName=TABLE_NAME, Item=item)
            logger.info(f"✅ DynamoDB 写入成功: {result.experiment_id}")
        except Exception as e:
            logger.error(f"DynamoDB 写入失败: {e}")
