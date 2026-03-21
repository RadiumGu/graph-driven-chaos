#!/usr/bin/env python3
"""
gen_template.py — 交互式 Chaos 实验模板生成器

从 Neptune 图谱读取服务上下文（Tier、调用关系），
智能填充稳态阈值、Stop Conditions、RCA 等，生成 YAML 实验模板。

用法：
  python gen_template.py
  python gen_template.py --service petsite --fault network_delay
  python gen_template.py --list-services
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from textwrap import dedent
from typing import Optional

# ─── Neptune 客户端 ──────────────────────────────────────────────────────────

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from runner.neptune_client import query_opencypher as _neptune_query


# ─── 图谱服务上下文 ───────────────────────────────────────────────────────────

class ServiceContext:
    """从 Neptune 拉取服务的 tier + 调用关系"""

    def __init__(self):
        self._services: dict[str, dict] = {}   # name → {tier, callers, callees}
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        # 节点
        rows = _neptune_query(
            "MATCH (m:Microservice) RETURN m.name AS name, m.recovery_priority AS tier"
        )
        for r in rows:
            self._services[r["name"]] = {"tier": r["tier"], "callers": [], "callees": []}
        # 边：调用关系
        calls = _neptune_query(
            "MATCH (a:Microservice)-[:Calls]->(b:Microservice) RETURN a.name AS src, b.name AS dst"
        )
        for c in calls:
            src, dst = c["src"], c["dst"]
            if src in self._services:
                self._services[src]["callees"].append(dst)
            if dst in self._services:
                self._services[dst]["callers"].append(src)
        self._loaded = True

    def list_services(self) -> list[str]:
        self.load()
        return sorted(self._services.keys())

    def get(self, name: str) -> Optional[dict]:
        self.load()
        return self._services.get(name)

    def tier(self, name: str) -> str:
        ctx = self.get(name)
        return ctx["tier"] if ctx else "Tier1"

    def callers(self, name: str) -> list[str]:
        ctx = self.get(name)
        return ctx["callers"] if ctx else []

    def callees(self, name: str) -> list[str]:
        ctx = self.get(name)
        return ctx["callees"] if ctx else []


# ─── 故障类型元数据 ───────────────────────────────────────────────────────────

FAULT_TYPES = {
    # Pod 故障
    "pod_kill":          {"label": "Pod Kill",           "category": "pod",     "duration": "2m"},
    "pod_failure":       {"label": "Pod Failure",        "category": "pod",     "duration": "3m"},
    "container_kill":    {"label": "Container Kill",     "category": "pod",     "duration": "2m"},
    # 网络故障
    "network_delay":     {"label": "Network Delay",      "category": "network", "duration": "3m"},
    "network_loss":      {"label": "Network Loss",       "category": "network", "duration": "3m"},
    "network_corrupt":   {"label": "Network Corrupt",    "category": "network", "duration": "3m"},
    "network_duplicate": {"label": "Network Duplicate",  "category": "network", "duration": "3m"},
    "network_bandwidth": {"label": "Network Bandwidth",  "category": "network", "duration": "3m"},
    "network_partition": {"label": "Network Partition",  "category": "network", "duration": "2m"},
    # 应用/协议故障
    "http_chaos":        {"label": "HTTP Chaos",         "category": "app",     "duration": "3m"},
    "dns_chaos":         {"label": "DNS Chaos",          "category": "app",     "duration": "2m"},
    "io_chaos":          {"label": "IO Chaos",           "category": "app",     "duration": "3m"},
    "time_chaos":        {"label": "Time Chaos",         "category": "app",     "duration": "3m"},
    # 资源压力
    "pod_cpu_stress":    {"label": "Pod CPU Stress",     "category": "stress",  "duration": "5m"},
    "pod_memory_stress": {"label": "Pod Memory Stress",  "category": "stress",  "duration": "5m"},
    "kernel_chaos":      {"label": "Kernel Chaos",       "category": "kernel",  "duration": "2m"},
}

FAULT_MENU = list(FAULT_TYPES.keys())


# ─── Tier 推断规则 ─────────────────────────────────────────────────────────────

TIER_CONFIG = {
    "Tier0": {
        "before_sr":    95,   # 注入前稳态成功率要求
        "after_sr":     95,   # 恢复后稳态成功率要求
        "after_p99":    5000, # 恢复后 p99 延迟要求 (ms)
        "stop_sr":      50,   # Stop Condition 触发阈值
        "stop_p99":     8000,
        "rca":          True,
    },
    "Tier1": {
        "before_sr":    90,
        "after_sr":     90,
        "after_p99":    8000,
        "stop_sr":      30,
        "stop_p99":     15000,
        "rca":          True,
    },
    "Tier2": {
        "before_sr":    80,
        "after_sr":     80,
        "after_p99":    15000,
        "stop_sr":      20,
        "stop_p99":     30000,
        "rca":          False,
    },
}

# fault category → 故障期间对成功率影响预期（不应触发 stop condition 的宽容度）
FAULT_TOLERANCE = {
    "pod":     0.40,   # Pod Kill 期间成功率可能降 40%
    "network": 0.30,
    "stress":  0.20,
    "app":     0.30,
    "kernel":  0.50,
}


# ─── 参数提示 ─────────────────────────────────────────────────────────────────

def prompt_fault_params(fault_type: str) -> dict:
    """根据 fault_type 交互式询问额外参数"""
    params = {}
    cat = FAULT_TYPES[fault_type]["category"]

    # 通用：mode / value
    print(f"\n  [故障模式] 支持: one / all / fixed-percent / random-max-percent")
    mode = input("  mode [fixed-percent]: ").strip() or "fixed-percent"
    params["mode"] = mode

    if mode in ("fixed-percent", "random-max-percent"):
        value = input("  value（百分比，如 50）[50]: ").strip() or "50"
        params["value"] = value
    elif mode == "fixed":
        value = input("  value（Pod 数量，如 1）[1]: ").strip() or "1"
        params["value"] = value
    else:
        params["value"] = ""

    # 持续时间
    default_dur = FAULT_TYPES[fault_type]["duration"]
    dur = input(f"  duration [{default_dur}]: ").strip() or default_dur
    params["duration"] = dur

    # 类型专属参数
    if fault_type == "network_delay":
        latency = input("  latency（如 100ms / 500ms）[100ms]: ").strip() or "100ms"
        jitter  = input("  jitter [0ms]: ").strip() or "0ms"
        params["latency"] = latency
        params["jitter"]  = jitter

    elif fault_type == "network_loss":
        loss = input("  loss（丢包率 %，如 30）[30]: ").strip() or "30"
        params["loss"] = loss

    elif fault_type == "network_corrupt":
        corrupt = input("  corrupt（损坏率 %，如 20）[20]: ").strip() or "20"
        params["corrupt"] = corrupt

    elif fault_type == "network_duplicate":
        dup = input("  duplicate（重复率 %，如 20）[20]: ").strip() or "20"
        params["duplicate"] = dup

    elif fault_type == "network_bandwidth":
        rate = input("  rate（如 1mbps / 100kbps）[1mbps]: ").strip() or "1mbps"
        params["rate"] = rate

    elif fault_type == "network_partition":
        direction = input("  direction (to/from/both) [to]: ").strip() or "to"
        ext = input("  external_targets（逗号分隔 IP/域名，空则只隔离 K8s 内）: ").strip()
        params["direction"] = direction
        params["external_targets"] = [t.strip() for t in ext.split(",") if t.strip()]

    elif fault_type == "pod_cpu_stress":
        workers = input("  workers [2]: ").strip() or "2"
        load    = input("  load（每 worker CPU%，如 80）[80]: ").strip() or "80"
        params["workers"] = int(workers)
        params["load"]    = int(load)

    elif fault_type == "pod_memory_stress":
        size = input("  size（如 256MB / 50%）[256MB]: ").strip() or "256MB"
        params["size"] = size

    elif fault_type == "http_chaos":
        action = input("  action (delay/abort/replace) [delay]: ").strip() or "delay"
        port   = input("  port [80]: ").strip() or "80"
        params["action"] = action
        params["port"]   = int(port)
        if action == "delay":
            delay = input("  delay（如 1s / 500ms）[1s]: ").strip() or "1s"
            params["delay"] = delay

    elif fault_type == "dns_chaos":
        action  = input("  action (error/random) [error]: ").strip() or "error"
        pattern = input("  patterns（逗号分隔，如 *.example.com，空则匹配所有）: ").strip()
        params["action"]   = action
        params["patterns"] = [p.strip() for p in pattern.split(",") if p.strip()]

    elif fault_type == "io_chaos":
        action      = input("  action (latency/fault/attrOverride) [latency]: ").strip() or "latency"
        volume_path = input("  volume_path [/]: ").strip() or "/"
        delay       = input("  delay（如 100ms）[100ms]: ").strip() or "100ms"
        params["action"]      = action
        params["volume_path"] = volume_path
        params["delay"]       = delay

    elif fault_type == "time_chaos":
        offset = input("  time_offset（如 -5m / +1h / 100ms）[-5m]: ").strip() or "-5m"
        params["time_offset"] = offset

    elif fault_type == "kernel_chaos":
        print("  kernel_chaos 较危险，建议仅在 Tier2 服务使用")
        failtype = input("  failtype (0=slab_alloc 1=alloc_pages 2=bio) [0]: ").strip() or "0"
        params["fail_kern_request"] = {
            "callchain": [{"funcname": "alloc_pages"}],
            "failtype": int(failtype),
            "probability": 1,
            "times": 1,
        }

    return params


# ─── YAML 模板生成 ────────────────────────────────────────────────────────────

def build_fault_block(fault_type: str, params: dict) -> str:
    lines = [f"  type: {fault_type}"]
    lines.append(f"  mode: {params['mode']}")
    lines.append(f"  value: \"{params['value']}\"")
    lines.append(f"  duration: \"{params['duration']}\"")

    # 类型专属字段
    for key in ["latency", "jitter", "loss", "corrupt", "duplicate", "rate",
                "direction", "workers", "load", "size", "action", "port",
                "delay", "volume_path", "time_offset"]:
        if key in params:
            v = params[key]
            if isinstance(v, str):
                lines.append(f"  {key}: \"{v}\"")
            else:
                lines.append(f"  {key}: {v}")

    if "external_targets" in params and params["external_targets"]:
        lines.append("  external_targets:")
        for t in params["external_targets"]:
            lines.append(f"    - \"{t}\"")

    if "patterns" in params and params["patterns"]:
        lines.append("  patterns:")
        for p in params["patterns"]:
            lines.append(f"    - \"{p}\"")

    if "fail_kern_request" in params:
        fkr = params["fail_kern_request"]
        lines.append("  fail_kern_request:")
        lines.append(f"    failtype: {fkr['failtype']}")
        lines.append(f"    probability: {fkr['probability']}")
        lines.append(f"    times: {fkr['times']}")
        lines.append("    callchain:")
        for cc in fkr.get("callchain", []):
            lines.append(f"      - funcname: \"{cc['funcname']}\"")

    return "\n".join(lines)


def generate_yaml(
    service: str,
    fault_type: str,
    params: dict,
    tier: str,
    callers: list[str],
    callees: list[str],
) -> str:
    tc  = TIER_CONFIG.get(tier, TIER_CONFIG["Tier1"])
    cat = FAULT_TYPES[fault_type]["category"]
    tol = FAULT_TOLERANCE.get(cat, 0.30)

    # Stop Condition 成功率阈值 = 注入前要求 × (1 - tolerance)
    stop_sr = max(int(tc["before_sr"] * (1 - tol)), tc["stop_sr"])

    exp_name = f"{service}-{fault_type.replace('_', '-')}-{tier.lower()}"
    ts       = datetime.now().strftime("%Y%m%d")

    # 调用关系描述
    dep_comment = ""
    if callers:
        dep_comment += f"# 上游调用方: {', '.join(callers)}\n"
    if callees:
        dep_comment += f"# 下游依赖:   {', '.join(callees)}\n"

    fault_block = build_fault_block(fault_type, params)

    yaml_content = f"""\
# 自动生成 by gen_template.py — {datetime.now().strftime('%Y-%m-%d %H:%M')}
# Neptune 图谱上下文: tier={tier}, callers=[{', '.join(callers)}], callees=[{', '.join(callees)}]
{dep_comment}
name: {exp_name}-{ts}
description: "{FAULT_TYPES[fault_type]['label']} on {service}（{tier}）— 验证弹性与可观测性"

target:
  service: {service}
  namespace: default
  tier: {tier}

fault:
{fault_block}

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
    threshold: "< {stop_sr}%"
    window: "30s"
    action: abort
  - metric: latency_p99
    threshold: "> {tc['stop_p99']}ms"
    window: "30s"
    action: abort

rca:
  enabled: {str(tc['rca']).lower()}
  trigger_after: "30s"
  expected_root_cause: {service}

graph_feedback:
  enabled: true
  edges:
    - Calls

options:
  max_duration: "10m"
  save_to_bedrock_kb: false
"""
    return yaml_content


# ─── 交互式入口 ───────────────────────────────────────────────────────────────

def interactive(preset_service: str = None, preset_fault: str = None):
    ctx = ServiceContext()

    print("\n" + "═" * 60)
    print("  🧪 Chaos 实验模板生成器（Neptune 智能上下文）")
    print("═" * 60)

    # 1. 选择服务
    try:
        services = ctx.list_services()
    except Exception as e:
        print(f"⚠️  无法连接 Neptune，使用离线模式: {e}")
        services = ["petsite", "petsearch", "payforadoption",
                    "pethistory", "petlistadoptions", "petstatusupdater", "trafficgenerator"]

    print("\n📦 可用服务:")
    for i, svc in enumerate(services, 1):
        info = ctx.get(svc)
        tier = info["tier"] if info else "?"
        print(f"  {i:2}. {svc:<25} [{tier}]")

    if preset_service and preset_service in services:
        service = preset_service
        print(f"\n  ✅ 使用服务: {service}")
    else:
        while True:
            sel = input("\n请选择服务（序号或名称）: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(services):
                service = services[int(sel) - 1]
                break
            elif sel in services:
                service = sel
                break
            else:
                print("  ❌ 无效选择，请重试")

    # 从 Neptune 拉取上下文
    tier    = ctx.tier(service)
    callers = ctx.callers(service)
    callees = ctx.callees(service)

    print(f"\n  📊 Neptune 上下文:")
    print(f"     Tier:    {tier}")
    print(f"     上游:    {', '.join(callers) if callers else '无'}")
    print(f"     下游依赖: {', '.join(callees) if callees else '无'}")

    # 2. 选择故障类型
    print("\n💥 故障类型:")
    cat_order = ["pod", "network", "app", "stress", "kernel"]
    cats = {}
    for k, v in FAULT_TYPES.items():
        cats.setdefault(v["category"], []).append(k)

    idx = 1
    idx_map = {}
    for cat in cat_order:
        if cat not in cats:
            continue
        cat_label = {"pod": "Pod 故障", "network": "网络故障", "app": "应用/协议",
                     "stress": "资源压力", "kernel": "内核故障"}[cat]
        print(f"\n  [{cat_label}]")
        for k in cats[cat]:
            print(f"  {idx:2}. {k:<25} {FAULT_TYPES[k]['label']}")
            idx_map[idx] = k
            idx += 1

    if preset_fault and preset_fault in FAULT_TYPES:
        fault_type = preset_fault
        print(f"\n  ✅ 使用故障类型: {fault_type}")
    else:
        while True:
            sel = input("\n请选择故障类型（序号或名称）: ").strip()
            if sel.isdigit() and int(sel) in idx_map:
                fault_type = idx_map[int(sel)]
                break
            elif sel in FAULT_TYPES:
                fault_type = sel
                break
            else:
                print("  ❌ 无效选择，请重试")

    # 3. 收集故障参数
    print(f"\n⚙️  配置 {FAULT_TYPES[fault_type]['label']} 参数（回车使用默认值）:")
    params = prompt_fault_params(fault_type)

    # 4. 生成 YAML
    yaml_content = generate_yaml(
        service=service,
        fault_type=fault_type,
        params=params,
        tier=tier,
        callers=callers,
        callees=callees,
    )

    # 5. 确定保存路径
    tier_dir = {"Tier0": "tier0", "Tier1": "tier1", "Tier2": "tier2"}.get(tier, "tier1")
    out_dir  = f"/home/ubuntu/tech/chaos/code/experiments/{tier_dir}"
    os.makedirs(out_dir, exist_ok=True)

    exp_name = f"{service}-{fault_type.replace('_', '-')}"
    out_path = f"{out_dir}/{exp_name}.yaml"

    print(f"\n{'─' * 60}")
    print("📄 生成的模板预览:")
    print("─" * 60)
    print(yaml_content)
    print("─" * 60)

    while True:
        action = input(f"\n💾 保存到 {out_path}？(y/n/自定义路径) [y]: ").strip() or "y"
        if action.lower() == "y":
            with open(out_path, "w") as f:
                f.write(yaml_content)
            print(f"✅ 已保存: {out_path}")
            print(f"\n运行命令:")
            print(f"  cd /home/ubuntu/tech/chaos/code")
            print(f"  python main.py run --file experiments/{tier_dir}/{exp_name}.yaml")
            print(f"  python main.py run --file experiments/{tier_dir}/{exp_name}.yaml --dry-run  # 先 dry-run")
            break
        elif action.lower() == "n":
            print("已取消保存。")
            break
        else:
            # 自定义路径
            out_path = action
            with open(out_path, "w") as f:
                f.write(yaml_content)
            print(f"✅ 已保存: {out_path}")
            break


def main():
    parser = argparse.ArgumentParser(description="Chaos 实验模板生成器")
    parser.add_argument("--service", help="目标服务名（跳过交互选择）")
    parser.add_argument("--fault",   help="故障类型（跳过交互选择）")
    parser.add_argument("--list-services", action="store_true", help="列出所有服务")
    args = parser.parse_args()

    if args.list_services:
        ctx = ServiceContext()
        print("\n可用服务（来自 Neptune 图谱）:")
        for svc in ctx.list_services():
            info = ctx.get(svc)
            tier    = info["tier"]
            callers = info["callers"]
            callees = info["callees"]
            print(f"  {svc:<25} [{tier}]  上游: {callers}  下游: {callees}")
        return

    interactive(preset_service=args.service, preset_fault=args.fault)


if __name__ == "__main__":
    main()
