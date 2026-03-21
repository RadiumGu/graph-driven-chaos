"""
target_resolver.py - 运行时目标解析器

将 YAML 中的逻辑服务名（service_name + resource_type）解析为真实 AWS ARN（FIS）
或验证 K8s Pod 存在（Chaos Mesh）。

优先级（FIS）：本地缓存 (TTL=1h) → Neptune 图谱（OpenCypher + Gremlin + SigV4）→ AWS API 兜底

支持的 resource_type（FIS）：
  lambda:function  — service_name 匹配 Lambda 函数名片段
  rds:cluster      — service_name 匹配 Microservice Neptune 节点名，走 DependsOn 边
  eks:nodegroup    — service_name 为 EKS 集群名（如 PetSite）
  ec2:subnet       — service_name 为 AZ 名（如 ap-northeast-1a）
  ec2:volume       — service_name 为 AZ 名，查找挂载到 EKS 节点的 EBS 卷
  ec2:instance     — service_name 匹配 EC2 实例标签 Name

缓存文件（审计留底）：
  targets-fis.json       — FIS 实验目标（ARN）
  targets-chaosmesh.json — Chaos Mesh 实验目标（Pod 列表）
"""
from __future__ import annotations

import glob as _glob
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import boto3
import yaml as _yaml

from .config import REGION, ACCOUNT_ID, SERVICE_TO_K8S_LABEL
from .neptune_client import query_opencypher, query_gremlin

if TYPE_CHECKING:
    from .experiment import Experiment

logger = logging.getLogger(__name__)

# ─── 缓存文件路径 ──────────────────────────────────────────────────────────────

_BASE          = os.path.join(os.path.dirname(__file__), "..")
FIS_CACHE_FILE = os.path.join(_BASE, "targets-fis.json")
CM_CACHE_FILE  = os.path.join(_BASE, "targets-chaosmesh.json")
# 向后兼容旧路径
CACHE_FILE     = FIS_CACHE_FILE

CACHE_TTL = 3600  # 1 小时


class TargetResolver:
    """
    运行时目标解析器。
    - FIS:        resolve(service_name, resource_type) → ARN string
    - Chaos Mesh: resolve_chaosmesh_target(service, namespace) → dict
    - 批量:       resolve_all_experiments(experiments_dir) → {"fis": ..., "chaosmesh": ...}
    """

    def __init__(self, tags: dict = None):
        self._tags: dict             = tags or {}
        self._fis_cache: dict        = {}
        self._fis_cache_loaded: bool = False
        self._cm_cache: dict         = {}
        self._cm_cache_loaded: bool  = False

    # ─── Tag 过滤 ──────────────────────────────────────────────────────────────

    def _match_tags(self, resource_arn: str) -> bool:
        """检查资源 tag 是否匹配所有 filter tag（resourcegroupstaggingapi）"""
        if not self._tags:
            return True
        try:
            client = boto3.client("resourcegroupstaggingapi", region_name=REGION)
            resp = client.get_resources(ResourceARNList=[resource_arn])
            mappings = resp.get("ResourceTagMappingList", [])
            if not mappings:
                return False
            tags = {t["Key"]: t["Value"] for t in mappings[0].get("Tags", [])}
            return all(tags.get(k) == v for k, v in self._tags.items())
        except Exception as e:
            logger.warning(f"Tag 检查失败（放行）: {resource_arn}: {e}")
            return True

    # ─── Neptune 查询（委托给 neptune_client.py）─────────────────────────────

    def _neptune_query(self, cypher: str) -> list[dict]:
        """OpenCypher 查询，委托给统一 neptune_client。"""
        return query_opencypher(cypher)

    def _neptune_gremlin_query(self, gremlin: str) -> list:
        """Gremlin 查询，委托给统一 neptune_client。"""
        return query_gremlin(gremlin)

    # ─── Neptune 解析层 ────────────────────────────────────────────────────────

    def _resolve_from_neptune(self, service_name: str, resource_type: str) -> Optional[str]:
        try:
            if resource_type == "lambda:function":
                rows = self._neptune_query(
                    f"MATCH (m:Microservice)-[:DependsOn]->(l:LambdaFunction) "
                    f"WHERE m.name CONTAINS '{service_name}' OR l.name CONTAINS '{service_name}' "
                    f"RETURN l.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

            elif resource_type == "rds:cluster":
                # 先试 OpenCypher DependsOn 边
                rows = self._neptune_query(
                    f"MATCH (m:Microservice {{name: '{service_name}'}})-[:DependsOn]->(r:RDSCluster) "
                    f"RETURN r.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

                # OpenCypher 无结果时改用 Gremlin（DependsOn 边 OpenCypher 可能查不到）
                vals = self._neptune_gremlin_query(
                    f"g.V().has('Microservice','name','{service_name}')"
                    f".out('DependsOn').hasLabel('RDSCluster').values('arn')"
                )
                if vals:
                    return str(vals[0])

            elif resource_type == "eks:nodegroup":
                rows = self._neptune_query(
                    f"MATCH (e:EKSCluster {{name: '{service_name}'}}) RETURN e.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    arn = rows[0]["arn"]
                    if ":nodegroup/" in arn:
                        return arn

            elif resource_type == "ec2:subnet":
                rows = self._neptune_query(
                    f"MATCH (s:Subnet {{availability_zone: '{service_name}'}}) "
                    f"RETURN s.arn AS arn LIMIT 1"
                )
                if rows and rows[0].get("arn"):
                    return rows[0]["arn"]

        except Exception as e:
            logger.warning(f"Neptune 查询失败，将走 AWS API 兜底: {e}")

        return None

    # ─── AWS API 兜底 ──────────────────────────────────────────────────────────

    def _resolve_from_aws(self, service_name: str, resource_type: str) -> Optional[str]:
        try:
            if resource_type == "lambda:function":
                return self._find_lambda_arn(service_name)
            elif resource_type == "rds:cluster":
                return self._find_rds_cluster_arn(service_name)
            elif resource_type == "eks:nodegroup":
                return self._find_eks_nodegroup_arn(service_name)
            elif resource_type == "ec2:subnet":
                return self._find_subnet_arn(service_name)
            elif resource_type == "ec2:volume":
                return self._find_ebs_volume_arn(service_name)
            elif resource_type == "ec2:instance":
                return self._find_instance_arn(service_name)
        except Exception as e:
            logger.error(f"AWS API 解析失败 [{resource_type}/{service_name}]: {e}")
        return None

    def _find_lambda_arn(self, service_name: str) -> Optional[str]:
        lam = boto3.client("lambda", region_name=REGION)
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                if service_name.lower() in fn["FunctionName"].lower():
                    if self._tags and not self._match_tags(fn["FunctionArn"]):
                        continue
                    return fn["FunctionArn"]
        return None

    def _find_rds_cluster_arn(self, service_name: str) -> Optional[str]:
        rds = boto3.client("rds", region_name=REGION)
        resp = rds.describe_db_clusters()
        clusters = resp.get("DBClusters", [])

        # 排除 grafana / neptune 等非应用集群（避免误匹配 grafana-aurora-mysql）
        EXCLUDE = ("grafana", "neptune")
        candidates = [
            c for c in clusters
            if not any(x in c["DBClusterIdentifier"].lower() for x in EXCLUDE)
        ]

        # 先按服务名片段精确匹配（候选集中）
        for c in candidates:
            if service_name.lower() in c["DBClusterIdentifier"].lower():
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]

        # 优先返回 petsite / serviceseks2 相关集群
        PREFER = ("petsite", "serviceseks2")
        for c in candidates:
            cid = c["DBClusterIdentifier"].lower()
            if any(x in cid for x in PREFER) and c.get("Status") == "available":
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]

        # 兜底：第一个可用的非排除集群
        for c in candidates:
            if c.get("Status") == "available":
                if self._tags and not self._match_tags(c["DBClusterArn"]):
                    continue
                return c["DBClusterArn"]
        return None

    def _find_eks_nodegroup_arn(self, cluster_name: str) -> Optional[str]:
        eks = boto3.client("eks", region_name=REGION)
        resp = eks.list_nodegroups(clusterName=cluster_name)
        groups = resp.get("nodegroups", [])
        if not groups:
            return None
        for ng in groups:
            detail = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng)
            arn = detail["nodegroup"]["nodegroupArn"]
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_subnet_arn(self, az: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        # 优先找 EKS 工作子网（带 kubernetes.io 标签）
        for tag_key in ["kubernetes.io/cluster/PetSite", "kubernetes.io/role/internal-elb"]:
            resp = ec2.describe_subnets(Filters=[
                {"Name": "availabilityZone", "Values": [az]},
                {"Name": "tag-key",          "Values": [tag_key]},
            ])
            subnets = resp.get("Subnets", [])
            for s in subnets:
                arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:subnet/{s['SubnetId']}"
                if self._tags and not self._match_tags(arn):
                    continue
                return arn
        # 兜底：AZ 内第一个子网
        resp = ec2.describe_subnets(Filters=[
            {"Name": "availabilityZone", "Values": [az]},
        ])
        subnets = resp.get("Subnets", [])
        for s in subnets:
            arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:subnet/{s['SubnetId']}"
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_ebs_volume_arn(self, az: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        # 优先找挂载到 EKS 节点的卷（带 eks:cluster-name 标签）
        resp = ec2.describe_volumes(Filters=[
            {"Name": "availability-zone",    "Values": [az]},
            {"Name": "status",               "Values": ["in-use"]},
            {"Name": "tag:eks:cluster-name", "Values": ["PetSite"]},
        ])
        vols = resp.get("Volumes", [])
        if not vols:
            # 兜底：AZ 内任意已挂载卷
            resp = ec2.describe_volumes(Filters=[
                {"Name": "availability-zone", "Values": [az]},
                {"Name": "status",            "Values": ["in-use"]},
            ])
            vols = resp.get("Volumes", [])
        for v in vols:
            arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/{v['VolumeId']}"
            if self._tags and not self._match_tags(arn):
                continue
            return arn
        return None

    def _find_instance_arn(self, service_name: str) -> Optional[str]:
        ec2 = boto3.client("ec2", region_name=REGION)
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:Name",            "Values": [f"*{service_name}*"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{inst['InstanceId']}"
                if self._tags and not self._match_tags(arn):
                    continue
                return arn
        return None

    # ─── FIS 缓存层 ───────────────────────────────────────────────────────────

    def _load_fis_cache(self):
        if self._fis_cache_loaded:
            return
        self._fis_cache_loaded = True

        # 向后兼容：也检查旧的 targets.json
        old_cache = os.path.join(os.path.dirname(__file__), "..", "targets.json")
        for path in (FIS_CACHE_FILE, old_cache):
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                resolved_at = data.get("resolved_at", "")
                ttl         = data.get("ttl_seconds", CACHE_TTL)
                if resolved_at:
                    age = time.time() - datetime.fromisoformat(resolved_at).timestamp()
                    if age > ttl:
                        logger.info(f"FIS 目标缓存已过期（{age:.0f}s > {ttl}s），忽略")
                        continue
                self._fis_cache = data.get("targets", {})
                logger.debug(f"已加载 {len(self._fis_cache)} 条 FIS 缓存目标（来自 {path}）")
                return
            except Exception as e:
                logger.warning(f"FIS 缓存加载失败（忽略）: {e}")

    def _save_fis_cache(self):
        data = {
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": CACHE_TTL,
            "targets":     self._fis_cache,
        }
        try:
            with open(os.path.abspath(FIS_CACHE_FILE), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"FIS 缓存保存失败（非致命）: {e}")

    # ─── Chaos Mesh 缓存层 ────────────────────────────────────────────────────

    def _load_cm_cache(self):
        if self._cm_cache_loaded:
            return
        self._cm_cache_loaded = True
        if not os.path.exists(CM_CACHE_FILE):
            return
        try:
            with open(CM_CACHE_FILE) as f:
                data = json.load(f)
            self._cm_cache = data.get("targets", {})
            logger.debug(f"已加载 {len(self._cm_cache)} 条 Chaos Mesh 缓存目标")
        except Exception as e:
            logger.warning(f"Chaos Mesh 缓存加载失败（忽略）: {e}")

    def _save_cm_cache(self):
        data = {
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "targets":     self._cm_cache,
        }
        try:
            with open(os.path.abspath(CM_CACHE_FILE), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Chaos Mesh 缓存保存失败（非致命）: {e}")

    # ─── Chaos Mesh Pod 解析 ──────────────────────────────────────────────────

    def resolve_chaosmesh_target(self, service: str, namespace: str = "default") -> dict:
        """
        解析 Chaos Mesh 目标（K8s Pod），写入 targets-chaosmesh.json，返回：
        {
            "service": "petsearch",
            "namespace": "default",
            "label_selector": "app=petsearch",
            "pods": [
                {"name": "petsearch-xxx", "status": "Running", "ip": "10.x.x.x", "node": "ip-..."}
            ],
            "replicas": 2,
            "resolved_at": "2026-03-20T..."
        }
        """
        self._load_cm_cache()
        cache_key = f"{service}:{namespace}"

        # 逻辑名 → K8s app label 映射
        k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)

        pods     = self._kubectl_get_pods(k8s_label, namespace)
        replicas = self._kubectl_get_replicas(k8s_label, namespace)

        entry = {
            "service":        service,
            "k8s_app_label":  k8s_label,
            "namespace":      namespace,
            "label_selector": f"app={k8s_label}",
            "pods":           pods,
            "replicas":       replicas,
            "resolved_at":    datetime.now(timezone.utc).isoformat(),
        }

        # 保留已有的 experiments 关联列表
        if cache_key in self._cm_cache and "experiments" in self._cm_cache[cache_key]:
            entry["experiments"] = self._cm_cache[cache_key]["experiments"]

        self._cm_cache[cache_key] = entry
        self._save_cm_cache()

        logger.info(
            f"Chaos Mesh 目标已解析: {service}/{namespace} → "
            f"{len(pods)} pods, replicas={replicas}"
        )
        return entry

    def _kubectl_get_pods(self, service: str, namespace: str) -> list[dict]:
        """kubectl get pods -l app=<service> → [{name, status, ip, node}]"""
        try:
            r = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace,
                 "-l", f"app={service}", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            items = json.loads(r.stdout or "{}").get("items", [])
            pods = []
            for pod in items:
                meta   = pod.get("metadata", {})
                status = pod.get("status", {})
                phase  = status.get("phase", "Unknown")
                if phase in ("Succeeded", "Completed"):
                    continue
                pods.append({
                    "name":   meta.get("name", ""),
                    "status": phase,
                    "ip":     status.get("podIP", ""),
                    "node":   pod.get("spec", {}).get("nodeName", ""),
                })
            return pods
        except Exception as e:
            logger.warning(f"kubectl get pods 失败 [{service}/{namespace}]: {e}")
            return []

    def _kubectl_get_replicas(self, service: str, namespace: str) -> int:
        """kubectl get deployment <service> → spec.replicas"""
        try:
            r = subprocess.run(
                ["kubectl", "get", "deployment", service,
                 "-n", namespace, "-o", "jsonpath={.spec.replicas}"],
                capture_output=True, text=True, timeout=10,
            )
            val = r.stdout.strip()
            return int(val) if val.isdigit() else 0
        except Exception as e:
            logger.warning(f"kubectl get deployment 失败 [{service}/{namespace}]: {e}")
            return 0

    # ─── 公共接口（FIS ARN 解析）──────────────────────────────────────────────

    def resolve(self, service_name: str, resource_type: str) -> Optional[str]:
        """
        解析 FIS 目标：service_name + resource_type → ARN。
        优先级：FIS 缓存 → Neptune → AWS API
        """
        self._load_fis_cache()
        cache_key = f"{resource_type}:{service_name}"

        if cache_key in self._fis_cache:
            logger.debug(f"FIS 缓存命中: {cache_key}")
            return self._fis_cache[cache_key]["arn"]

        arn    = self._resolve_from_neptune(service_name, resource_type)
        source = "neptune"
        if not arn:
            arn    = self._resolve_from_aws(service_name, resource_type)
            source = "aws-api"

        if arn:
            self._fis_cache[cache_key] = {"arn": arn, "resolved_from": source}
            self._save_fis_cache()
            logger.info(f"ARN 已解析（{source}）: {cache_key} → {arn}")
        else:
            logger.warning(f"无法解析 ARN: {cache_key}")

        return arn

    def refresh(self):
        """清除所有本地缓存（FIS + Chaos Mesh），强制重新解析"""
        self._fis_cache        = {}
        self._fis_cache_loaded = True
        self._cm_cache         = {}
        self._cm_cache_loaded  = True
        old_cache = os.path.join(os.path.dirname(__file__), "..", "targets.json")
        for path in (FIS_CACHE_FILE, CM_CACHE_FILE, old_cache):
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"已清除缓存文件: {path}")

    def resolve_experiment(self, experiment: "Experiment") -> None:
        """
        填充 Experiment.fault.extra_params 中缺失的 ARN 字段。
        读取 extra_params.service_name + extra_params.resource_type，
        解析后写入对应的 ARN key（function_arn / cluster_arn / nodegroup_arn 等）。
        失败时仅记录警告，不抛出异常。
        """
        extra = experiment.fault.extra_params
        if not extra:
            return

        service_name  = extra.get("service_name")
        resource_type = extra.get("resource_type")
        if not service_name or not resource_type:
            return

        try:
            arn = self.resolve(service_name, resource_type)
        except Exception as e:
            logger.warning(f"实验 {experiment.name}: ARN 解析异常（跳过）: {e}")
            return

        if not arn:
            logger.warning(
                f"实验 {experiment.name}: 无法解析 {resource_type}/{service_name}，"
                f"将使用 YAML 中已有 ARN（如有）"
            )
            return

        # 按 resource_type 写入对应的 ARN key
        if resource_type == "lambda:function":
            extra["function_arn"] = arn
        elif resource_type == "rds:cluster":
            extra["cluster_arn"] = arn
        elif resource_type == "eks:nodegroup":
            extra["nodegroup_arn"] = arn
        elif resource_type == "ec2:subnet":
            extra["subnet_arn"] = arn
        elif resource_type == "ec2:volume":
            extra.setdefault("volume_arns", [])
            if arn not in extra["volume_arns"]:
                extra["volume_arns"].append(arn)
        elif resource_type == "ec2:instance":
            extra["instance_arn"] = arn

        logger.info(f"实验 {experiment.name}: {resource_type} → {arn}")

    def resolve_all_experiments(self, experiments_dir: str) -> dict:
        """
        批量解析所有实验目标，按 backend 分别写入：
        - targets-fis.json       (FIS ARN 审计)
        - targets-chaosmesh.json (Chaos Mesh Pod 审计)
        返回 {"fis": {...}, "chaosmesh": {...}}
        """
        self._load_fis_cache()
        self._load_cm_cache()

        fis_results: dict = {}
        cm_results:  dict = {}

        pattern    = os.path.join(experiments_dir, "**", "*.yaml")
        yaml_files = sorted(_glob.glob(pattern, recursive=True))

        for path in yaml_files:
            try:
                with open(path) as f:
                    d = _yaml.safe_load(f)
            except Exception as e:
                logger.warning(f"跳过 YAML（读取失败）: {path}: {e}")
                continue

            if not d or not d.get("enabled", True):
                continue

            backend  = d.get("backend", "chaosmesh")
            target   = d.get("target", {})
            exp_name = d.get("name", os.path.basename(path))

            if backend == "fis":
                extra     = (d.get("fault") or {}).get("extra_params") or {}
                svc_name  = extra.get("service_name", "")
                res_type  = extra.get("resource_type", "")
                if not svc_name or not res_type:
                    continue

                cache_key = f"{res_type}:{svc_name}"
                arn       = self.resolve(svc_name, res_type)
                source    = self._fis_cache.get(cache_key, {}).get("resolved_from", "unknown")

                fis_results[cache_key] = {
                    "arn":           arn,
                    "resolved_from": source,
                    "experiment":    exp_name,
                }
                logger.debug(f"FIS 目标: {cache_key} → {arn} ({source})")

            else:
                # Chaos Mesh：解析 Pod 目标
                svc = target.get("service", "")
                ns  = target.get("namespace", "default")
                if not svc:
                    continue

                cache_key = f"{svc}:{ns}"
                try:
                    entry = self.resolve_chaosmesh_target(svc, ns)
                except Exception as e:
                    logger.warning(f"Chaos Mesh 目标解析失败 [{svc}/{ns}]: {e}")
                    k8s_label = SERVICE_TO_K8S_LABEL.get(svc, svc)
                    entry = {
                        "service": svc, "namespace": ns,
                        "k8s_app_label": k8s_label,
                        "label_selector": f"app={k8s_label}",
                        "pods": [], "replicas": 0,
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    }

                if cache_key not in cm_results:
                    cm_results[cache_key] = entry.copy()
                    cm_results[cache_key].setdefault("experiments", [])

                if exp_name not in cm_results[cache_key]["experiments"]:
                    cm_results[cache_key]["experiments"].append(exp_name)

                # 同步回 cm_cache（带 experiments 列表）
                self._cm_cache[cache_key] = cm_results[cache_key]

        # 写入最终 Chaos Mesh 缓存（包含 experiments 字段）
        self._save_cm_cache()

        return {"fis": fis_results, "chaosmesh": cm_results}

    # ─── 基础设施快照（供 HypothesisAgent 使用）────────────────────────────────

    def get_infra_snapshot(self, services: list[str], namespace: str = "default") -> dict:
        """
        为 HypothesisAgent 提供实时基础设施快照。

        对每个逻辑服务名，解析当前 K8s Pod 状态 + FIS 可用资源，
        返回轻量级摘要，供 LLM 生成更精准的假设。

        返回格式：
        {
            "petsite": {
                "k8s": {"replicas": 2, "running_pods": 2, "nodes": ["ip-10-..."]},
                "aws_resources": {"lambda:function": "arn:aws:...", ...},
            },
            ...
        }
        """
        snapshot = {}
        self._load_fis_cache()

        for service in services:
            entry: dict = {"k8s": None, "aws_resources": {}}

            # K8s Pod 状态
            try:
                k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)
                pods = self._kubectl_get_pods(k8s_label, namespace)
                replicas = self._kubectl_get_replicas(k8s_label, namespace)
                running = [p for p in pods if p.get("status") == "Running"]
                nodes = list({p.get("node", "") for p in running if p.get("node")})
                entry["k8s"] = {
                    "app_label": k8s_label,
                    "replicas": replicas,
                    "running_pods": len(running),
                    "total_pods": len(pods),
                    "nodes": nodes,
                }
            except Exception as e:
                logger.debug(f"K8s 快照跳过 {service}: {e}")

            # FIS 可用资源（从缓存中查找关联的 ARN）
            for cache_key, cached in self._fis_cache.items():
                # cache_key 格式: "resource_type:service_name"
                parts = cache_key.split(":", 1)
                if len(parts) == 2:
                    res_type_prefix = parts[0]
                    svc_part = cache_key.split(":")[-1] if ":" in cache_key else ""
                    # 宽松匹配：服务名出现在 cache_key 中
                    if service.lower() in cache_key.lower():
                        arn = cached.get("arn", "")
                        if arn:
                            entry["aws_resources"][cache_key] = arn

            snapshot[service] = entry

        logger.info(f"基础设施快照: {len(snapshot)} 个服务")
        return snapshot
