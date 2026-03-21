"""
chaos_mcp.py - ChaosMesh 故障注入客户端

改用 kubectl apply YAML 方式直接注入，绕过 chaosmesh Python 库的
_wait_experiment_injection 阻塞问题和重试 409 bug。
- 立即返回，不等待 Injected 状态
- 支持所有 fault_type（PodChaos / NetworkChaos / 其他）
- 通过 kubectl delete 清理
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from typing import Optional

import yaml

from .config import SERVICE_TO_K8S_LABEL

logger = logging.getLogger(__name__)

NAMESPACE = "default"
CHAOS_GROUP = "chaos-mesh.org/v1alpha1"


def _gen_name(prefix: str) -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}"


def _apply_yaml(manifest: dict) -> dict:
    """kubectl apply -f <tmp.yaml>，返回 manifest（含 name）"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(manifest, f)
        tmp = f.name
    try:
        r = subprocess.run(
            ['kubectl', 'apply', '-f', tmp],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            raise RuntimeError(f"kubectl apply 失败: {r.stderr.strip()}")
        logger.info(f"kubectl apply: {r.stdout.strip()}")
        return manifest
    finally:
        os.unlink(tmp)


def _selector_spec(service: str, namespace: str = "default") -> dict:
    # 逻辑名 → K8s app label（单一数据源：config.SERVICE_TO_K8S_LABEL）
    k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)
    return {
        "namespaces": [namespace],
        "labelSelectors": {"app": k8s_label},
    }


class ChaosMCPClient:
    """
    kubectl-based ChaosMesh 故障注入客户端。
    直接 apply Chaos Mesh CRD YAML，无阻塞，支持全部 fault_type。
    """

    def inject(self, fault_type: str, service: str, namespace: str,
               duration: str, mode: str, value: str, **kwargs) -> dict:
        """
        注入故障，返回包含 metadata.name 的 manifest dict。
        注入前验证 Pod 存在，并将目标信息写入 targets-chaosmesh.json 审计留底。
        """
        # 目标验证：确认 Pod 存在，写入审计记录
        from .target_resolver import TargetResolver
        _target = TargetResolver().resolve_chaosmesh_target(service, namespace)
        if not _target.get("pods"):
            raise RuntimeError(
                f"目标服务 {service}/{namespace} 无 Running Pods"
                f"（label app={service}），注入中止"
            )

        builders = {
            "pod_kill":          self._pod_chaos,
            "pod_failure":       self._pod_chaos,
            "container_kill":    self._pod_chaos,
            "pod_cpu_stress":    self._stress_chaos,
            "pod_memory_stress": self._stress_chaos,
            "network_delay":     self._network_chaos,
            "network_loss":      self._network_chaos,
            "network_corrupt":   self._network_chaos,
            "network_partition": self._network_chaos,
            "network_duplicate": self._network_chaos,
            "http_chaos":        self._http_chaos,
            "io_chaos":          self._io_chaos,
            "time_chaos":        self._time_chaos,
            "kernel_chaos":      self._kernel_chaos,
        }
        builder = builders.get(fault_type)
        if not builder:
            raise ValueError(f"未知 fault_type: {fault_type!r}")

        manifest = builder(
            fault_type=fault_type, service=service, namespace=namespace,
            duration=duration, mode=mode, value=value, **kwargs
        )
        logger.info(f"注入: {fault_type} → {service} name={manifest['metadata']['name']}")
        return _apply_yaml(manifest)

    # ── PodChaos ────────────────────────────────────────────────────────────

    def _pod_chaos(self, fault_type: str, service: str, namespace: str,
                   duration: str, mode: str, value: str, **kwargs) -> dict:
        action_map = {
            "pod_kill":     "pod-kill",
            "pod_failure":  "pod-failure",
            "container_kill": "container-kill",
        }
        spec: dict = {
            "action":   action_map[fault_type],
            "mode":     mode,
            "selector": _selector_spec(service, namespace),
            "duration": duration,
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        if fault_type == "container_kill":
            spec["containerNames"] = kwargs.get("container_names") or ["main"]

        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "PodChaos",
            "metadata": {"name": _gen_name("pod-kill" if fault_type == "pod_kill"
                                           else fault_type.replace("_", "-")),
                         "namespace": namespace},
            "spec": spec,
        }

    # ── StressChaos ─────────────────────────────────────────────────────────

    def _stress_chaos(self, fault_type: str, service: str, namespace: str,
                      duration: str, mode: str, value: str, **kwargs) -> dict:
        if fault_type == "pod_cpu_stress":
            stressors = {"cpu": {
                "workers": kwargs.get("workers") or 1,
                "load":    kwargs.get("load") or 50,
            }}
        else:
            stressors = {"memory": {
                "workers": 1,
                "size": kwargs.get("size") or "256MB",
            }}
        spec: dict = {
            "mode":      mode,
            "selector":  _selector_spec(service, namespace),
            "stressors": stressors,
            "duration":  duration,
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "StressChaos",
            "metadata": {"name": _gen_name("stress"), "namespace": namespace},
            "spec": spec,
        }

    # ── NetworkChaos ────────────────────────────────────────────────────────

    def _network_chaos(self, fault_type: str, service: str, namespace: str,
                       duration: str, mode: str, value: str, **kwargs) -> dict:
        action_map = {
            "network_delay":     "delay",
            "network_loss":      "loss",
            "network_corrupt":   "corrupt",
            "network_partition": "partition",
            "network_duplicate": "duplicate",
        }
        action = action_map[fault_type]
        spec: dict = {
            "action":    action,
            "mode":      mode,
            "selector":  _selector_spec(service, namespace),
            "duration":  duration,
            "direction": kwargs.get("direction") or "to",
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        if fault_type == "network_delay":
            spec["delay"] = {
                "latency":     kwargs.get("latency") or "100ms",
                "jitter":      kwargs.get("jitter") or "0ms",
                "correlation": kwargs.get("correlation") or "0",
            }
        elif fault_type == "network_loss":
            spec["loss"] = {
                "loss":        kwargs.get("loss") or "50",
                "correlation": "0",
            }
        elif fault_type == "network_corrupt":
            spec["corrupt"] = {
                "corrupt":     kwargs.get("corrupt") or "50",
                "correlation": "0",
            }
        elif fault_type == "network_duplicate":
            spec["duplicate"] = {"duplicate": "50", "correlation": "0"}
        if kwargs.get("external_targets"):
            spec["externalTargets"] = kwargs["external_targets"]

        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "NetworkChaos",
            "metadata": {"name": _gen_name(f"net-{action}"), "namespace": namespace},
            "spec": spec,
        }

    # ── HTTPChaos ───────────────────────────────────────────────────────────

    def _http_chaos(self, fault_type: str, service: str, namespace: str,
                    duration: str, mode: str, value: str, **kwargs) -> dict:
        spec: dict = {
            "mode":     mode,
            "selector": _selector_spec(service, namespace),
            "target":   kwargs.get("target") or "Request",
            "port":     kwargs.get("port") or 80,
            "duration": duration,
        }
        action = kwargs.get("action") or "delay"
        if action == "delay":
            spec["delay"] = kwargs.get("http_delay") or "1s"
        elif action == "abort":
            spec["abort"] = True
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "HTTPChaos",
            "metadata": {"name": _gen_name("http"), "namespace": namespace},
            "spec": spec,
        }

    # ── IOChaos ─────────────────────────────────────────────────────────────

    def _io_chaos(self, fault_type: str, service: str, namespace: str,
                  duration: str, mode: str, value: str, **kwargs) -> dict:
        spec: dict = {
            "action":     kwargs.get("io_action") or "latency",
            "mode":       mode,
            "selector":   _selector_spec(service, namespace),
            "volumePath": kwargs.get("volume_path") or "/",
            "path":       "**/*",
            "duration":   duration,
            "delay":      kwargs.get("io_delay") or "100ms",
            "percent":    100,
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "IOChaos",
            "metadata": {"name": _gen_name("io"), "namespace": namespace},
            "spec": spec,
        }

    # ── TimeChaos ───────────────────────────────────────────────────────────

    def _time_chaos(self, fault_type: str, service: str, namespace: str,
                    duration: str, mode: str, value: str, **kwargs) -> dict:
        spec: dict = {
            "mode":       mode,
            "selector":   _selector_spec(service, namespace),
            "timeOffset": kwargs.get("time_offset") or "-5m",
            "duration":   duration,
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "TimeChaos",
            "metadata": {"name": _gen_name("time"), "namespace": namespace},
            "spec": spec,
        }

    # ── KernelChaos ─────────────────────────────────────────────────────────

    def _kernel_chaos(self, fault_type: str, service: str, namespace: str,
                      duration: str, mode: str, value: str, **kwargs) -> dict:
        spec: dict = {
            "mode":     mode,
            "selector": _selector_spec(service, namespace),
            "duration": duration,
            "failKernRequest": kwargs.get("fail_kern_request") or {
                "callchain": [{"funcname": "alloc_pages"}],
                "failtype": 0, "probability": 1, "times": 1,
            },
        }
        if mode in ("fixed", "fixed-percent", "random-max-percent"):
            spec["value"] = str(value)
        return {
            "apiVersion": CHAOS_GROUP,
            "kind": "KernelChaos",
            "metadata": {"name": _gen_name("kernel"), "namespace": namespace},
            "spec": spec,
        }

    # ── 公共工具 ─────────────────────────────────────────────────────────────

    def delete(self, chaos_type: str, name: str, namespace: str = "default") -> bool:
        """kubectl delete 删除实验"""
        if not name:
            return True
        crd_map = {
            "POD_KILL": "podchaos", "POD_FAILURE": "podchaos",
            "CONTAINER_KILL": "podchaos",
            "POD_STRESS_CPU": "stresschaos", "POD_STRESS_MEMORY": "stresschaos",
            "NetworkChaos": "networkchaos", "HTTPChaos": "httpchaos",
            "IOChaos": "iochaos", "TimeChaos": "timechaos",
            "KernelChaos": "kernelchaos", "StressChaos": "stresschaos",
            # 兜底：小写
            "podchaos": "podchaos", "networkchaos": "networkchaos",
        }
        crd = crd_map.get(chaos_type, chaos_type.lower().replace("chaos", "chaos"))
        try:
            r = subprocess.run(
                ["kubectl", "delete", crd, name, "-n", namespace, "--ignore-not-found"],
                capture_output=True, text=True, timeout=15,
            )
            logger.info(f"✅ kubectl delete {crd}/{name}: {r.stdout.strip()}")
            return True
        except Exception as e:
            logger.error(f"kubectl delete 失败: {e}")
            return False

    def list_experiments(self) -> list:
        """列出当前所有活跃实验（Pre-flight 用）"""
        active = []
        crds = ["podchaos", "networkchaos", "stresschaos",
                "httpchaos", "iochaos", "timechaos", "kernelchaos"]
        for crd in crds:
            try:
                r = subprocess.run(
                    ["kubectl", "get", crd, "-n", "default",
                     "-o", "jsonpath={.items[*].metadata.name}"],
                    capture_output=True, text=True, timeout=5,
                )
                names = r.stdout.strip().split()
                active.extend({"name": n, "kind": crd} for n in names if n)
            except Exception:
                pass
        return active

    def extract_experiment_name(self, result: dict, fault_type: str = "") -> str:
        """从 inject() 返回的 manifest 提取实验名"""
        meta = result.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("name"):
            return meta["name"]
        return ""

    # fault_type → delete 所用的 chaos_type 参数
    FAULT_TO_DELETE_TYPE = {
        "pod_kill":          "POD_KILL",
        "pod_failure":       "POD_FAILURE",
        "container_kill":    "CONTAINER_KILL",
        "pod_cpu_stress":    "POD_STRESS_CPU",
        "pod_memory_stress": "POD_STRESS_MEMORY",
        "network_delay":     "NetworkChaos",
        "network_loss":      "NetworkChaos",
        "network_corrupt":   "NetworkChaos",
        "network_partition": "NetworkChaos",
        "network_duplicate": "NetworkChaos",
        "http_chaos":        "HTTPChaos",
        "io_chaos":          "IOChaos",
        "time_chaos":        "TimeChaos",
        "kernel_chaos":      "KernelChaos",
    }

    def close(self):
        pass   # kubectl-based，无需关闭进程

    def check_pods(self, service: str, namespace: str = "default") -> dict:
        """检查目标服务 Pod 健康状态（Phase 0 / Phase 4 用）"""
        try:
            import json as _json
            r = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace,
                 "-l", f"app={service}", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            items = _json.loads(r.stdout or "{}").get("items", [])
            total, running = 0, 0
            not_running = []
            for pod in items:
                pod_name = pod["metadata"]["name"]
                phase    = pod.get("status", {}).get("phase", "Unknown")
                if phase in ("Succeeded", "Completed"):
                    continue
                total += 1
                cs    = pod.get("status", {}).get("containerStatuses", [])
                ready = all(c.get("ready", False) for c in cs) if cs else False
                if phase == "Running" and ready:
                    running += 1
                else:
                    not_running.append({"pod": pod_name, "phase": phase, "ready": ready})
            return {"total": total, "running": running, "not_running": not_running}
        except Exception as e:
            logger.warning(f"check_pods 失败: {e}")
            return {"total": 0, "running": 0, "not_running": []}
