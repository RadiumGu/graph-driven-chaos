"""
hypothesis_agent.py — AI 驱动的混沌工程假设生成器

从 Neptune 图谱查询拓扑，调用 Bedrock LLM 生成假设并排序，
输出 hypotheses.json + 兼容现有 runner 的实验 YAML。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3

from .models import Hypothesis

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from runner.config import BEDROCK_REGION, BEDROCK_MODEL
from runner.neptune_client import query_gremlin_parsed, parse_graphson

logger = logging.getLogger(__name__)

HYPOTHESES_PATH = os.path.join(os.path.dirname(__file__), "..", "hypotheses.json")

# ─── 故障类型 → YAML 参数映射 ────────────────────────────────────────────────

FAULT_DEFAULTS = {
    "pod_kill":          {"mode": "fixed-percent", "value": "50", "duration": "2m"},
    "pod_failure":       {"mode": "fixed-percent", "value": "50", "duration": "3m"},
    "network_delay":     {"mode": "all", "value": "", "duration": "3m", "latency": "200ms"},
    "network_loss":      {"mode": "all", "value": "", "duration": "3m", "loss": "30"},
    "network_partition": {"mode": "all", "value": "", "duration": "2m", "direction": "both", "external_targets": []},
    "pod_cpu_stress":    {"mode": "all", "value": "", "duration": "5m", "workers": 2, "load": 80},
    "pod_memory_stress": {"mode": "all", "value": "", "duration": "5m", "size": "256MB"},
    "dns_chaos":         {"mode": "all", "value": "", "duration": "2m", "action": "error"},
    "http_chaos":        {"mode": "all", "value": "", "duration": "3m", "action": "delay", "port": 80, "delay": "1s"},
}

TIER_CONFIG = {
    "Tier0": {"before_sr": 95, "after_sr": 95, "after_p99": 5000, "stop_sr": 50, "stop_p99": 8000, "rca": True},
    "Tier1": {"before_sr": 90, "after_sr": 90, "after_p99": 8000, "stop_sr": 30, "stop_p99": 15000, "rca": True},
    "Tier2": {"before_sr": 80, "after_sr": 80, "after_p99": 15000, "stop_sr": 20, "stop_p99": 30000, "rca": False},
}

VALID_FAULT_TYPES = list(FAULT_DEFAULTS.keys())


# ─── Neptune Gremlin 客户端（委托给 neptune_client.py）──────────────────────

def _gremlin_query(gremlin: str) -> list:
    """执行 Neptune Gremlin 查询，返回解析后的 Python 对象列表。"""
    return query_gremlin_parsed(gremlin)


# ─── Bedrock LLM 调用 ────────────────────────────────────────────────────────

def _invoke_llm(prompt: str, max_tokens: int = 8192) -> str:
    """调用 Bedrock Claude 模型，返回文本响应。
    使用 boto3 内置 adaptive retry 模式（exponential backoff），
    自动处理 ThrottlingException / ServiceUnavailableException。
    """
    from botocore.config import Config as _BotocoreConfig
    client = boto3.client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=_BotocoreConfig(retries={"mode": "adaptive", "max_attempts": 5}),
    )
    resp = client.invoke_model(
        modelId=BEDROCK_MODEL,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"]


def _extract_json(text: str) -> list | dict:
    """从 LLM 响应中提取 JSON（处理 markdown code fence）。"""
    # 尝试找 ```json ... ``` 块
    import re
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    return json.loads(raw)


# ─── HypothesisAgent ─────────────────────────────────────────────────────────

class HypothesisAgent:
    """
    AI 驱动的混沌工程假设生成器。

    输入源：Neptune 图谱拓扑 + DynamoDB 历史实验
    输出：hypotheses.json（假设库，含优先级排序）
    """

    def __init__(self):
        self._topology = None
        self._incidents = None
        self._infra_snapshot = None

    # ── Neptune 图谱查询 ─────────────────────────────────────────────

    def _query_topology(self) -> list[dict]:
        """获取完整服务依赖图（improvement-plan.md Gremlin 模板 #1）。"""
        if self._topology is not None:
            return self._topology
        gremlin = """
        g.V().hasLabel('Microservice')
          .project('name','tier','deps','callers','resources')
          .by('name')
          .by('recovery_priority')
          .by(out('DependsOn').values('name').fold())
          .by(in('DependsOn').values('name').fold())
          .by(out('RunsOn','DependsOn').hasLabel('LambdaFunction','RDSCluster','DynamoDBTable','SQSQueue').values('name').fold())
        """
        self._topology = _gremlin_query(gremlin)
        logger.info(f"Neptune 拓扑: {len(self._topology)} 个服务")
        return self._topology

    def _query_incidents(self) -> list[dict]:
        """获取历史故障模式（improvement-plan.md Gremlin 模板 #2）。"""
        if self._incidents is not None:
            return self._incidents
        gremlin = """
        g.V().hasLabel('Incident')
          .project('service','type','duration','impact')
          .by(out('AffectedService').values('name'))
          .by('incident_type')
          .by('duration_minutes')
          .by('impact_level')
        """
        try:
            self._incidents = _gremlin_query(gremlin)
        except Exception as e:
            logger.warning(f"历史故障查询失败（可能无 Incident 节点）: {e}")
            self._incidents = []
        return self._incidents

    def _query_history(self, service: Optional[str] = None) -> list[dict]:
        """从 DynamoDB 查询历史实验结果。"""
        from runner.query import ExperimentQueryClient
        client = ExperimentQueryClient()
        if service:
            return client.list_by_service(service, days=180, limit=50)
        # 查所有已知服务
        results = []
        for svc in self._query_topology():
            results.extend(client.list_by_service(svc["name"], days=180, limit=10))
        return results

    def _query_infra_snapshot(self, services: list[str]) -> dict:
        """通过 TargetResolver 获取实时基础设施快照（Pod 数量、节点、AWS 资源 ARN）。"""
        if self._infra_snapshot is not None:
            return self._infra_snapshot
        try:
            from runner.target_resolver import TargetResolver
            resolver = TargetResolver()
            self._infra_snapshot = resolver.get_infra_snapshot(services)
            logger.info(f"基础设施快照: {len(self._infra_snapshot)} 个服务")
        except Exception as e:
            logger.warning(f"基础设施快照获取失败（非致命，将用拓扑信息代替）: {e}")
            self._infra_snapshot = {}
        return self._infra_snapshot

    # ── 生成假设 ─────────────────────────────────────────────────────

    def generate(
        self,
        max_hypotheses: int = 50,
        service_filter: Optional[str] = None,
    ) -> list[Hypothesis]:
        """
        生成假设主流程：
        1. Neptune 查询拓扑 + 历史故障
        2. DynamoDB 查询历史实验
        3. Bedrock LLM 生成假设
        4. 解析 + 构建 Hypothesis 对象
        """
        topology = self._query_topology()
        if service_filter:
            topology = [s for s in topology if s["name"] == service_filter]
            if not topology:
                raise ValueError(f"服务 {service_filter} 不在 Neptune 图谱中")

        incidents = self._query_incidents()
        history = self._query_history(service_filter)

        # 获取实时基础设施快照（Pod 数量、节点分布、AWS 资源）
        service_names = [s["name"] for s in topology]
        infra_snapshot = self._query_infra_snapshot(service_names)

        # 构建 LLM prompt
        prompt = self._build_generate_prompt(topology, incidents, history, max_hypotheses, infra_snapshot)
        logger.info("调用 Bedrock LLM 生成假设...")
        llm_response = _invoke_llm(prompt)

        # 解析 LLM 输出
        raw_list = _extract_json(llm_response)
        hypotheses = []
        for i, item in enumerate(raw_list[:max_hypotheses]):
            h = Hypothesis(
                id=item.get("id", f"H{i+1:03d}"),
                title=item.get("title", ""),
                description=item.get("description", ""),
                steady_state=item.get("steady_state", ""),
                fault_scenario=item.get("fault_scenario", ""),
                expected_impact=item.get("expected_impact", ""),
                failure_domain=item.get("failure_domain", "compute"),
                target_services=item.get("target_services", []),
                target_resources=item.get("target_resources", []),
                backend=item.get("backend", "chaosmesh"),
                source_context={"topology_services": len(topology), "incidents": len(incidents)},
            )
            hypotheses.append(h)

        logger.info(f"LLM 生成 {len(hypotheses)} 个假设")
        return hypotheses

    def _build_generate_prompt(
        self, topology: list, incidents: list, history: list, max_hypotheses: int,
        infra_snapshot: dict = None,
    ) -> str:
        # 简化 history 为摘要
        history_summary = []
        for item in history[:30]:
            history_summary.append({
                "service": item.get("target_service", {}).get("S", ""),
                "fault": item.get("fault_type", {}).get("S", ""),
                "status": item.get("status", {}).get("S", ""),
            })

        valid_faults_str = ", ".join(VALID_FAULT_TYPES)

        # 构建基础设施快照描述
        infra_section = ""
        if infra_snapshot:
            infra_section = f"""
## 实时基础设施状态（来自 TargetResolver）
{json.dumps(infra_snapshot, ensure_ascii=False, indent=2)}

注意：
- 利用实际 Pod 数量决定 fault mode/value（例如 2 副本服务用 fixed:1 而非 fixed-percent:50）
- 单节点部署的服务，pod_kill 会导致完全不可用，需要更保守的实验参数
- 有 AWS 资源（Lambda/RDS）的服务可以同时设计 FIS 实验
- 没有 running Pod 的服务跳过 Chaos Mesh 类假设
"""

        return f"""你是混沌工程假设专家。基于以下服务拓扑和依赖关系，生成混沌工程假设。

## 服务拓扑
{json.dumps(topology, ensure_ascii=False, indent=2)}
{infra_section}
## 历史实验结果
{json.dumps(history_summary, ensure_ascii=False, indent=2)}

## 历史故障事件
{json.dumps(incidents, ensure_ascii=False, indent=2)}

## 要求
1. 覆盖 5 大故障域：compute, data, network, dependencies, resources
2. 每个假设必须包含：稳态假设、故障场景、预期影响、验证标准
3. 优先关注 Tier0/Tier1 服务
4. 考虑级联故障（A 依赖 B，B 故障时 A 如何表现）
5. 避免生成已经验证过的重复假设
6. 最多生成 {max_hypotheses} 个假设
7. fault_scenario 中的故障类型必须是以下之一: {valid_faults_str}
8. backend 字段: 对 Lambda/RDS 等 AWS 资源用 "fis"，对 K8s Pod 用 "chaosmesh"
9. 按 JSON 数组格式输出，每个元素包含以下字段:
   id, title, description, steady_state, fault_scenario, expected_impact,
   failure_domain, target_services (数组), target_resources (数组), backend

```json
[
  {{
    "id": "H001",
    "title": "示例标题",
    "description": "详细描述",
    "steady_state": "成功率 >= 99%",
    "fault_scenario": "pod_kill 50% petsite Pod",
    "expected_impact": "成功率短暂下降后自动恢复",
    "failure_domain": "compute",
    "target_services": ["petsite"],
    "target_resources": ["pod"],
    "backend": "chaosmesh"
  }}
]
```"""

    # ── 优先级排序 ───────────────────────────────────────────────────

    def prioritize(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """
        用 LLM 对假设进行多维度评分：
        business_impact, blast_radius, feasibility, learning_value
        每项 1-10 分，加权求和后排序。
        """
        if not hypotheses:
            return hypotheses

        summaries = [
            {"id": h.id, "title": h.title, "failure_domain": h.failure_domain,
             "target_services": h.target_services, "backend": h.backend}
            for h in hypotheses
        ]

        prompt = f"""你是混沌工程优先级评估专家。对以下假设进行多维度评分。

## 假设列表
{json.dumps(summaries, ensure_ascii=False, indent=2)}

## 评分维度（每项 1-10 分）
- business_impact: 业务影响（Tier0 服务 > Tier1 > Tier2）
- blast_radius: 爆炸半径（越小越安全，分数越高越好，即小半径=高分）
- feasibility: 技术可行性（有现成 FIS action / Chaos Mesh CRD 的优先）
- learning_value: 学习价值（未测试过的故障模式优先）

## 输出格式
JSON 数组，每个元素: {{"id": "H001", "business_impact": 8, "blast_radius": 7, "feasibility": 9, "learning_value": 8}}

```json
[...]
```"""

        logger.info("调用 Bedrock LLM 评估优先级...")
        llm_response = _invoke_llm(prompt, max_tokens=4096)
        scores_list = _extract_json(llm_response)

        # 构建 id → scores 映射
        scores_map = {s["id"]: s for s in scores_list if "id" in s}

        for h in hypotheses:
            s = scores_map.get(h.id, {})
            h.priority_scores = {
                "business_impact": s.get("business_impact", 5),
                "blast_radius": s.get("blast_radius", 5),
                "feasibility": s.get("feasibility", 5),
                "learning_value": s.get("learning_value", 5),
            }
            # 加权总分（越高越优先）
            ps = h.priority_scores
            total = (ps["business_impact"] * 3 + ps["blast_radius"] * 1
                     + ps["feasibility"] * 2 + ps["learning_value"] * 2)
            h.priority = total

        # 按总分降序排序，分配 priority 排名
        hypotheses.sort(key=lambda h: h.priority, reverse=True)
        for rank, h in enumerate(hypotheses, 1):
            h.priority = rank

        return hypotheses

    # ── 导出实验 YAML ────────────────────────────────────────────────

    def to_experiment_yamls(
        self,
        hypotheses: list[Hypothesis],
        output_dir: str = "experiments/generated",
    ) -> list[str]:
        """将假设转化为兼容 runner load_experiment() 的 YAML 文件。"""
        os.makedirs(output_dir, exist_ok=True)
        paths = []

        for h in hypotheses:
            service = h.target_services[0] if h.target_services else "unknown"
            fault_type = self._extract_fault_type(h.fault_scenario)
            tier = self._infer_tier(service)
            tc = TIER_CONFIG.get(tier, TIER_CONFIG["Tier1"])

            exp_name = f"{service}-{fault_type.replace('_', '-')}-{h.id.lower()}"
            ts = datetime.now().strftime("%Y%m%d")
            defaults = FAULT_DEFAULTS.get(fault_type, FAULT_DEFAULTS["pod_kill"])

            # 构建 fault block
            fault_lines = [
                f"  type: {fault_type}",
                f"  mode: {defaults['mode']}",
                f'  value: "{defaults["value"]}"',
                f'  duration: "{defaults["duration"]}"',
            ]
            for k in ("latency", "loss", "direction", "action", "size"):
                if k in defaults:
                    v = defaults[k]
                    fault_lines.append(f'  {k}: "{v}"' if isinstance(v, str) else f"  {k}: {v}")
            for k in ("workers", "load", "port"):
                if k in defaults:
                    fault_lines.append(f"  {k}: {defaults[k]}")
            if "external_targets" in defaults and defaults["external_targets"]:
                fault_lines.append("  external_targets:")
                for t in defaults["external_targets"]:
                    fault_lines.append(f'    - "{t}"')

            yaml_content = f"""\
# 由 HypothesisAgent 生成 — {datetime.now().strftime('%Y-%m-%d %H:%M')}
# 假设 {h.id}: {h.title}
# 故障域: {h.failure_domain} | 后端: {h.backend}

name: {exp_name}-{ts}
description: "{h.title}"

target:
  service: {service}
  namespace: default
  tier: {tier}

fault:
{chr(10).join(fault_lines)}

steady_state:
  before:
    - metric: success_rate
      threshold: ">= {tc['before_sr']}%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= {tc['after_sr']}%"
      window: "5m"
    - metric: latency_p99
      threshold: "< {tc['after_p99']}ms"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< {tc['stop_sr']}%"
    window: "30s"
    action: abort
  - metric: latency_p99
    threshold: "> {tc['stop_p99']}ms"
    window: "30s"
    action: abort

rca:
  enabled: {str(tc['rca']).lower()}
  trigger_after: "30s"

graph_feedback:
  enabled: true
  edges:
    - Calls

backend: {h.backend}

options:
  max_duration: "10m"
  save_to_bedrock_kb: false
"""
            filename = f"{exp_name}.yaml"
            path = os.path.join(output_dir, filename)
            with open(path, "w") as f:
                f.write(yaml_content)
            paths.append(path)
            logger.info(f"已生成: {path}")

        return paths

    def _extract_fault_type(self, fault_scenario: str) -> str:
        """从 fault_scenario 描述中提取故障类型关键字。"""
        for ft in VALID_FAULT_TYPES:
            if ft in fault_scenario:
                return ft
        # fallback: 按关键词推断
        lower = fault_scenario.lower()
        if "kill" in lower or "崩溃" in lower:
            return "pod_kill"
        if "delay" in lower or "延迟" in lower:
            return "network_delay"
        if "loss" in lower or "丢包" in lower:
            return "network_loss"
        if "cpu" in lower:
            return "pod_cpu_stress"
        if "memory" in lower or "内存" in lower:
            return "pod_memory_stress"
        if "partition" in lower or "隔离" in lower:
            return "network_partition"
        if "dns" in lower:
            return "dns_chaos"
        return "pod_kill"

    def _infer_tier(self, service: str) -> str:
        """从已缓存的拓扑中推断服务 Tier。"""
        if self._topology:
            for svc in self._topology:
                if svc.get("name") == service:
                    return svc.get("tier", "Tier1")
        return "Tier1"

    # ── 持久化 ───────────────────────────────────────────────────────

    def save(self, hypotheses: list[Hypothesis], path: str = HYPOTHESES_PATH):
        """保存假设库到 hypotheses.json。"""
        topology = self._topology or []
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator": "hypothesis-agent-v1",
            "model": BEDROCK_MODEL,
            "graph_snapshot": {
                "services": len(topology),
            },
            "hypotheses": [h.to_dict() for h in hypotheses],
        }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"假设库已保存: {path} ({len(hypotheses)} 个假设)")

    @staticmethod
    def load(path: str = HYPOTHESES_PATH) -> list[Hypothesis]:
        """从 hypotheses.json 加载假设库。"""
        if not os.path.exists(path):
            return []
        with open(path) as f:
            data = json.load(f)
        return [Hypothesis.from_dict(h) for h in data.get("hypotheses", [])]
