"""
neptune_client.py - 统一 Neptune 访问客户端

消除 target_resolver.py / gen_template.py / fmea.py /
hypothesis_agent.py / graph_feedback.py 五处重复的 Neptune 连接代码。

支持两种查询方式：
  - OpenCypher: query_opencypher(cypher) → list[dict]
  - Gremlin:    query_gremlin(gremlin)   → list

认证：SigV4（boto3 Session）
SSL：CERT_NONE（Neptune 内网部署，VPC 内访问）
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .config import REGION, NEPTUNE_HOST, NEPTUNE_ENDPOINT

logger = logging.getLogger(__name__)

# 复用同一个 SSL context（禁用证书校验，Neptune 内网 VPC 访问）
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def query_opencypher(cypher: str) -> list[dict]:
    """
    执行 Neptune OpenCypher 查询，使用 SigV4 认证。

    Args:
        cypher: OpenCypher 查询字符串

    Returns:
        results 列表，每个元素为 dict

    Raises:
        Exception: 网络错误或 Neptune 返回错误时抛出
    """
    url = f"{NEPTUNE_ENDPOINT}/openCypher"
    session = boto3.Session(region_name=REGION)
    creds = session.get_credentials().get_frozen_credentials()
    body = json.dumps({"query": cypher})
    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={"Content-Type": "application/json", "Host": NEPTUNE_HOST},
    )
    SigV4Auth(creds, "neptune-db", REGION).add_auth(req)
    http_req = urllib.request.Request(
        url, data=body.encode(), headers=dict(req.headers), method="POST",
    )
    with urllib.request.urlopen(http_req, context=_ssl_ctx, timeout=10) as resp:
        return json.loads(resp.read()).get("results", [])


def query_gremlin(gremlin: str) -> list:
    """
    执行 Neptune Gremlin 查询，使用 SigV4 认证。
    自动解包 GraphSON 3.0 格式，返回 Python 原生类型列表。

    Args:
        gremlin: Gremlin traversal 字符串

    Returns:
        解包后的值列表（字符串 / 数字 / dict 等）

    Raises:
        Exception: 网络错误或 Neptune 返回错误时抛出
    """
    url = f"{NEPTUNE_ENDPOINT}/gremlin"
    session = boto3.Session(region_name=REGION)
    creds = session.get_credentials().get_frozen_credentials()
    body = json.dumps({"gremlin": gremlin})
    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={"Content-Type": "application/json", "Host": NEPTUNE_HOST},
    )
    SigV4Auth(creds, "neptune-db", REGION).add_auth(req)
    http_req = urllib.request.Request(
        url, data=body.encode(), headers=dict(req.headers), method="POST",
    )
    with urllib.request.urlopen(http_req, context=_ssl_ctx, timeout=10) as resp:
        raw = json.loads(resp.read())

    # GraphSON 3.0: result.data.@value 是列表
    data = raw.get("result", {}).get("data", {})
    if isinstance(data, dict):
        items = data.get("@value", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    # 每个元素可能是 {"@type": ..., "@value": <actual>} 或直接是值
    result = []
    for item in items:
        if isinstance(item, dict) and "@value" in item:
            result.append(item["@value"])
        else:
            result.append(item)
    return result


def parse_graphson(obj: Any) -> Any:
    """
    递归解析 GraphSON 格式为普通 Python 对象。
    供 agents/hypothesis_agent.py 等需要完整 GraphSON 解析的场景使用。

    Args:
        obj: GraphSON 原始对象（dict / list / scalar）

    Returns:
        Python 原生类型
    """
    if isinstance(obj, dict):
        t = obj.get("@type")
        v = obj.get("@value")
        if t == "g:List":
            return [parse_graphson(i) for i in v]
        if t == "g:Map":
            it = iter(v)
            return {parse_graphson(k): parse_graphson(val) for k, val in zip(it, it)}
        if t in ("g:Int32", "g:Int64", "g:Float", "g:Double"):
            return v
        if t and v is not None:
            return parse_graphson(v)
        return {k: parse_graphson(val) for k, val in obj.items()}
    if isinstance(obj, list):
        return [parse_graphson(i) for i in obj]
    return obj


def query_gremlin_parsed(gremlin: str) -> list:
    """
    执行 Gremlin 查询，返回完整 GraphSON 解析结果（list of Python objects）。
    适合 HypothesisAgent 等需要解析复杂 Map/List 结构的场景。

    Args:
        gremlin: Gremlin traversal 字符串

    Returns:
        解析后的 Python 对象列表
    """
    url = f"{NEPTUNE_ENDPOINT}/gremlin"
    session = boto3.Session(region_name=REGION)
    creds = session.get_credentials().get_frozen_credentials()
    body = json.dumps({"gremlin": gremlin})
    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={"Content-Type": "application/json", "Host": NEPTUNE_HOST},
    )
    SigV4Auth(creds, "neptune-db", REGION).add_auth(req)
    http_req = urllib.request.Request(
        url, data=body.encode(), headers=dict(req.headers), method="POST",
    )
    with urllib.request.urlopen(http_req, context=_ssl_ctx, timeout=15) as resp:
        raw = json.loads(resp.read())
    data = raw.get("result", {}).get("data", {})
    return parse_graphson(data)


def check_connectivity() -> bool:
    """
    验证 Neptune 连通性（执行轻量查询）。

    Returns:
        True 表示连通，False 表示不可达
    """
    try:
        query_opencypher("MATCH (n) RETURN n LIMIT 0")
        return True
    except Exception as e:
        logger.warning(f"Neptune 连通性检查失败: {e}")
        return False
