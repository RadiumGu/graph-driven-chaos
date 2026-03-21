"""
orchestrator.py — Workflow Orchestrator（批量编排）

执行策略：
  by_tier:     按 Tier 顺序（Tier2 → Tier1 → Tier0），低风险先行
  by_priority: 按假设优先级排序（需 hypotheses.json）
  by_domain:   按故障域分组执行
  full_suite:  按文件顺序全量执行
"""
from __future__ import annotations

import glob
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from runner import ExperimentRunner, load_experiment, Experiment, ExperimentResult

logger = logging.getLogger(__name__)

TIER_ORDER = {"Tier2": 0, "Tier1": 1, "Tier0": 2}

FAULT_DOMAIN_MAP = {
    "pod_kill": "compute", "pod_failure": "compute", "container_kill": "compute",
    "pod_cpu_stress": "resources", "pod_memory_stress": "resources",
    "network_delay": "network", "network_loss": "network",
    "network_corrupt": "network", "network_partition": "network",
    "network_duplicate": "network", "dns_chaos": "network",
    "http_chaos": "dependencies", "io_chaos": "resources",
    "time_chaos": "resources", "kernel_chaos": "resources",
}


@dataclass
class SuiteResult:
    strategy: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    aborted: int = 0
    errors: int = 0
    skipped: int = 0
    results: list[ExperimentResult] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    dry_run: bool = False

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"Suite 结果 — 策略: {self.strategy}",
            f"{'='*60}",
            f"总计: {self.total}  ✅ {self.passed}  ❌ {self.failed}  🛑 {self.aborted}  💥 {self.errors}  ⏭ {self.skipped}",
        ]
        for r in self.results:
            icon = {"PASSED": "✅", "FAILED": "❌", "ABORTED": "🛑", "ERROR": "💥"}.get(r.status, "⏭")
            lines.append(f"  {icon} {r.experiment.name} [{r.status}] {r.duration_seconds:.0f}s")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


class WorkflowOrchestrator:
    """实验批量编排器。"""

    def __init__(self, dry_run: bool = False, tags: dict = None):
        self.dry_run = dry_run
        self.tags = tags or {}

    # ── 公开接口 ─────────────────────────────────────────────────────

    def run_suite(
        self,
        experiment_paths: list[str],
        strategy: str = "by_tier",
        max_parallel: int = 1,
        stop_on_failure: bool = True,
        cooldown: int = 60,
        top: int | None = None,
        dry_run: bool = False,
    ) -> SuiteResult:
        dry = dry_run or self.dry_run
        result = SuiteResult(strategy=strategy, dry_run=dry,
                             start_time=datetime.now(timezone.utc).isoformat())

        # 加载 + 过滤
        experiments = self._load_all(experiment_paths)
        if not experiments:
            logger.warning("无可执行实验")
            result.end_time = datetime.now(timezone.utc).isoformat()
            return result

        # 排序
        experiments = self._sort(experiments, strategy)
        if top:
            experiments = experiments[:top]
        result.total = len(experiments)

        logger.info(f"Suite 启动: {result.total} 个实验, 策略={strategy}, parallel={max_parallel}, dry_run={dry}")

        # 执行
        if max_parallel <= 1:
            self._run_sequential(experiments, result, stop_on_failure, cooldown, dry)
        else:
            self._run_parallel(experiments, result, max_parallel, stop_on_failure, cooldown, dry)

        result.end_time = datetime.now(timezone.utc).isoformat()
        return result

    def generate_and_run(
        self,
        max_hypotheses: int = 20,
        top_n: int = 5,
        strategy: str = "by_priority",
        max_parallel: int = 1,
        stop_on_failure: bool = True,
        cooldown: int = 60,
        dry_run: bool = False,
    ) -> SuiteResult:
        """端到端: HypothesisAgent → 生成 YAML → 执行 suite。"""
        from agents import HypothesisAgent

        dry = dry_run or self.dry_run
        agent = HypothesisAgent()

        logger.info(f"Auto 模式: 生成最多 {max_hypotheses} 个假设, 取 top {top_n}")
        hypotheses = agent.generate(max_hypotheses=max_hypotheses)
        hypotheses = agent.prioritize(hypotheses)
        agent.save(hypotheses)

        top = hypotheses[:top_n]
        output_dir = os.path.join(os.path.dirname(__file__), "experiments", "generated")
        paths = agent.to_experiment_yamls(top, output_dir=output_dir)
        logger.info(f"已生成 {len(paths)} 个实验 YAML → {output_dir}")

        return self.run_suite(
            experiment_paths=paths,
            strategy=strategy,
            max_parallel=max_parallel,
            stop_on_failure=stop_on_failure,
            cooldown=cooldown,
            dry_run=dry,
        )

    # ── 内部方法 ─────────────────────────────────────────────────────

    def _load_all(self, paths: list[str]) -> list[Experiment]:
        experiments = []
        for p in paths:
            try:
                exp = load_experiment(p)
                if not exp.enabled:
                    logger.info(f"跳过 disabled: {p}")
                    continue
                experiments.append(exp)
            except Exception as e:
                logger.error(f"加载失败 {p}: {e}")
        return experiments

    def _sort(self, experiments: list[Experiment], strategy: str) -> list[Experiment]:
        if strategy == "by_tier":
            return sorted(experiments, key=lambda e: TIER_ORDER.get(e.target_tier, 1))
        if strategy == "by_priority":
            return self._sort_by_priority(experiments)
        if strategy == "by_domain":
            return sorted(experiments, key=lambda e: FAULT_DOMAIN_MAP.get(e.fault.type, "other"))
        return experiments  # full_suite

    def _sort_by_priority(self, experiments: list[Experiment]) -> list[Experiment]:
        """尝试从 hypotheses.json 匹配优先级，无匹配则按 tier 排序。"""
        from agents import HypothesisAgent
        hypotheses = HypothesisAgent.load()
        # 建立 hypothesis id → priority 映射
        prio_map = {h.id.lower(): h.priority for h in hypotheses}
        # 尝试从实验名中提取 hypothesis id (如 "...-h001")
        def get_priority(exp: Experiment) -> int:
            name_lower = exp.name.lower()
            for hid, prio in prio_map.items():
                if hid in name_lower:
                    return prio
            return 1000 + TIER_ORDER.get(exp.target_tier, 1)
        return sorted(experiments, key=get_priority)

    def _run_sequential(self, experiments, suite: SuiteResult,
                        stop_on_failure: bool, cooldown: int, dry_run: bool):
        for i, exp in enumerate(experiments):
            logger.info(f"\n[{i+1}/{suite.total}] {exp.name}")
            r = self._execute_one(exp, dry_run)
            suite.results.append(r)
            self._tally(suite, r)

            if stop_on_failure and r.status in ("FAILED", "ABORTED", "ERROR"):
                suite.skipped = suite.total - len(suite.results)
                logger.warning(f"stop_on_failure: 跳过剩余 {suite.skipped} 个实验")
                break

            if i < len(experiments) - 1 and cooldown > 0 and not dry_run:
                logger.info(f"冷却 {cooldown}s...")
                time.sleep(cooldown)

    def _run_parallel(self, experiments, suite: SuiteResult,
                      max_parallel: int, stop_on_failure: bool,
                      cooldown: int, dry_run: bool):
        stopped = False
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {}
            for idx, exp in enumerate(experiments):
                if stopped:
                    suite.skipped += 1
                    continue
                # 为每个并行实验分配独立 namespace，避免 preflight 相互干扰
                isolated_ns = f"chaos-parallel-{idx}"
                f = pool.submit(self._execute_one, exp, dry_run, isolated_ns)
                futures[f] = exp

            for f in as_completed(futures):
                r = f.result()
                suite.results.append(r)
                self._tally(suite, r)
                if stop_on_failure and r.status in ("FAILED", "ABORTED", "ERROR"):
                    stopped = True

        suite.skipped = suite.total - len(suite.results)

    def _execute_one(self, exp: Experiment, dry_run: bool,
                     namespace_override: str | None = None) -> ExperimentResult:
        if namespace_override:
            # 覆盖 namespace 以隔离并行实验（不修改原始 Experiment 对象）
            import dataclasses
            exp = dataclasses.replace(exp, target_namespace=namespace_override)
        runner = ExperimentRunner(dry_run=dry_run, tags=self.tags)
        return runner.run(exp)

    @staticmethod
    def _tally(suite: SuiteResult, r: ExperimentResult):
        s = r.status
        if s == "PASSED":   suite.passed += 1
        elif s == "FAILED": suite.failed += 1
        elif s == "ABORTED": suite.aborted += 1
        else:               suite.errors += 1


def collect_yamls(directory: str) -> list[str]:
    """递归收集目录下所有 .yaml 文件。"""
    return sorted(glob.glob(os.path.join(directory, "**", "*.yaml"), recursive=True))
