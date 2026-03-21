"""
config.py - 中心化配置

所有模块应从此处 import 常量，禁止在业务代码中硬编码 Region / Account ID / ARN。
环境变量优先，内置合理默认值。
"""
import os

# ─── AWS 基础 ─────────────────────────────────────────────────────────────────

REGION     = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "926093770964")

# ─── Neptune ──────────────────────────────────────────────────────────────────

NEPTUNE_HOST     = os.environ.get(
    "NEPTUNE_HOST",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
NEPTUNE_PORT     = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_ENDPOINT = f"https://{NEPTUNE_HOST}:{NEPTUNE_PORT}"

# ─── FIS ──────────────────────────────────────────────────────────────────────

FIS_ROLE_ARN  = os.environ.get(
    "FIS_ROLE_ARN",
    f"arn:aws:iam::{ACCOUNT_ID}:role/chaos-fis-experiment-role",
)
FIS_S3_BUCKET = os.environ.get("FIS_S3_BUCKET", f"chaos-fis-config-{ACCOUNT_ID}")

# ─── Bedrock ──────────────────────────────────────────────────────────────────

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL  = os.environ.get(
    "BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"
)

# ─── DynamoDB ─────────────────────────────────────────────────────────────────

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "chaos-experiments")
# 实验记录 TTL（秒）。默认 365 天，确保闭环学习跨年度数据不丢失。
# 可通过 CHAOS_RECORD_TTL_DAYS 环境变量覆盖（单位：天）。
CHAOS_RECORD_TTL_DAYS = int(os.environ.get("CHAOS_RECORD_TTL_DAYS", "365"))
CHAOS_RECORD_TTL_SECONDS = CHAOS_RECORD_TTL_DAYS * 86400

# ─── DeepFlow ClickHouse ──────────────────────────────────────────────────────
# DeepFlow 部署的 ClickHouse HTTP 端口（默认 8123）
# 生产环境通过环境变量覆盖；认证（DEEPFLOW_CH_USER/DEEPFLOW_CH_PASSWORD）
# 暂未实现，参见 README TODO。

DEEPFLOW_CH_HOST = os.environ.get("DEEPFLOW_CH_HOST", "11.0.2.30")
DEEPFLOW_CH_PORT = int(os.environ.get("DEEPFLOW_CH_PORT", "8123"))

# ─── 服务名映射 ───────────────────────────────────────────────────────────────
# Neptune 逻辑名 → K8s app label（Chaos Mesh YAML 使用逻辑名，K8s Deployment label 不同）
# 单一数据源：target_resolver.py 和 chaos_mcp.py 均从此处 import
SERVICE_TO_K8S_LABEL: dict[str, str] = {
    "petsearch":        "search-service",
    "payforadoption":   "pay-for-adoption",
    "petlistadoptions": "list-adoptions",
    # petsite → petsite（一致，无需映射）
    # pethistory → pethistory（一致，无需映射）
}
