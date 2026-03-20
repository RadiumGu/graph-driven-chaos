# chaos-automation

PetSite 微服务混沌工程自动化平台 — 5 Phase 实验引擎，支持 AWS FIS + Chaos Mesh 双后端。

## 架构概览

```
                    main.py (CLI 入口)
                        │
                        ▼
              ┌─── ExperimentRunner (5 Phase) ───┐
              │                                   │
    TargetResolver          Experiment YAML
    (Neptune → AWS API      (逻辑名，无硬编码 ARN)
     → targets-*.json)
              │                                   │
    ┌─────────┴─────────┐                         │
    │                   │                         │
FISClient          ChaosMCPClient                 │
(AWS FIS API)      (kubectl apply CRD)            │
    │                   │                         │
    ▼                   ▼                         ▼
targets-fis.json   targets-chaosmesh.json    DeepFlow Metrics
(审计留底)          (审计留底)                (观测 + Guardrails)
                                                  │
                                        ┌─────────┴─────────┐
                                        │                   │
                                    RCA Engine          Reporter
                                    (Lambda)        (Markdown + DDB)
```

## 环境依赖

### 运行时

| 依赖 | 版本要求 | 用途 |
|------|---------|------|
| Python | >= 3.12 | 运行时 |
| boto3 | >= 1.34 | AWS API（FIS / RDS / EKS / Lambda / CloudWatch / DynamoDB / S3） |
| PyYAML | >= 6.0 | 实验 YAML 解析 |
| requests | >= 2.28 | Neptune HTTP API |
| kubectl | >= 1.28 | Chaos Mesh CRD 注入 / Pod 状态查询 |
| aws-cli | >= 2.15 | FIS 基础设施配置脚本（`fis_setup.py`） |

### Python 依赖安装

```bash
pip install boto3 pyyaml requests
```

> 其余均为 Python 标准库（json / subprocess / logging / ssl / urllib 等）

### AWS 环境

| 资源 | 说明 |
|------|------|
| IAM Role | `chaos-fis-experiment-role` — FIS 实验执行角色（`fis_setup.py` 自动创建） |
| CloudWatch Alarms | `chaos-*` — 5 个 FIS Stop Condition 告警（`fis_setup.py` 自动创建） |
| S3 Bucket | `chaos-fis-config-{ACCOUNT_ID}` — FIS Lambda Extension 通信（`fis_setup.py` 自动创建） |
| DynamoDB Table | `chaos-experiments` — 实验历史记录（`main.py setup` 创建） |
| Neptune Cluster | `petsite-neptune` — 服务拓扑图谱（Target Resolver + RCA + Graph Feedback） |
| EKS Cluster | `PetSite` — 目标微服务集群 |
| Lambda | `petsite-rca-engine` — RCA 根因分析引擎 |
| DeepFlow | 可观测性平台 — 成功率 / 延迟指标采集 |

### Kubernetes 环境

| 依赖 | 说明 |
|------|------|
| Chaos Mesh | >= 2.6 | 部署在 EKS 集群，提供 PodChaos / NetworkChaos / StressChaos 等 CRD |
| kubeconfig | 已配置 PetSite 集群访问权限 |

### 环境变量（可选，有默认值）

```bash
export AWS_DEFAULT_REGION=ap-northeast-1        # 默认 ap-northeast-1
export AWS_ACCOUNT_ID=926093770964              # 默认 926093770964
export NEPTUNE_HOST=petsite-neptune.cluster-xxx # 默认 PetSite Neptune 端点
export FIS_ROLE_ARN=arn:aws:iam::xxx:role/xxx   # 默认自动构建
export FIS_S3_BUCKET=chaos-fis-config-xxx       # 默认自动构建
export BEDROCK_REGION=us-east-1                 # 报告生成用 Bedrock
export BEDROCK_MODEL=us.anthropic.claude-sonnet-4-20250514
```

## 目录结构

```
code/
├── main.py                     # CLI 入口（run / setup / history）
├── resolve_targets.py          # 目标解析 CLI 工具
├── gen_template.py             # 从 Neptune 图谱自动生成实验模板
│
├── runner/
│   ├── config.py               # 中心化配置（Region / Account / Neptune / FIS）
│   ├── target_resolver.py      # 运行时 ARN 解析（Neptune → AWS API → 缓存）
│   ├── experiment.py           # Experiment 数据模型 + YAML 解析
│   ├── runner.py               # 5 Phase 执行引擎
│   ├── fault_injector.py       # 故障注入抽象层（FIS / ChaosMesh 统一接口）
│   ├── fis_backend.py          # AWS FIS 后端（create_template → start_experiment）
│   ├── chaos_mcp.py            # Chaos Mesh 后端（kubectl apply CRD）
│   ├── metrics.py              # DeepFlow 指标采集
│   ├── rca.py                  # RCA 根因分析触发器
│   ├── report.py               # Markdown 报告 + DynamoDB 写入
│   ├── graph_feedback.py       # Neptune 图谱反馈（chaos_resilience_score）
│   ├── query.py                # DeepFlow 查询客户端
│   └── result.py               # ExperimentResult 数据模型
│
├── experiments/
│   ├── fis/                    # AWS FIS 实验（8 个）
│   │   ├── eks-node/           # EKS 节点终止
│   │   ├── lambda/             # Lambda 延迟/错误注入
│   │   ├── network-infra/      # 网络中断 + EBS IO 延迟
│   │   └── rds/                # Aurora 主从切换 / 实例重启
│   ├── tier0/                  # Chaos Mesh Tier0 实验（4 个）
│   ├── tier1/                  # Chaos Mesh Tier1 实验（3 个）
│   └── network/                # Chaos Mesh 网络实验
│
├── infra/
│   ├── fis_setup.py            # FIS 基础设施一键配置（IAM + Alarms + S3）
│   └── dynamodb_setup.py       # DynamoDB 建表
│
├── targets-fis.json            # FIS 目标 ARN 审计记录（自动生成）
├── targets-chaosmesh.json      # Chaos Mesh Pod 目标审计记录（自动生成）
│
└── fmea/
    └── fmea.py                 # FMEA 故障模式分析
```

## 快速开始

### 1. 初始化基础设施

```bash
# 创建 FIS IAM Role + CloudWatch Alarms + S3 Bucket
cd code/infra && python3 fis_setup.py

# 创建 DynamoDB 实验记录表
cd code && python3 main.py setup
```

### 2. 解析目标（预览 + 审计留底）

```bash
cd code

# 解析所有实验目标（FIS ARN + ChaosMesh Pod）
python3 resolve_targets.py

# 只看 FIS / 只看 ChaosMesh
python3 resolve_targets.py --backend fis
python3 resolve_targets.py --backend chaosmesh

# 强制刷新（清除缓存重新解析）
python3 resolve_targets.py --refresh
```

### 3. 执行实验

```bash
cd code

# Chaos Mesh 实验
python3 main.py run --file experiments/tier0/petsite-pod-kill.yaml

# FIS 实验
python3 main.py run --file experiments/fis/rds/fis-aurora-failover.yaml

# Dry-run（不注入，走完 5 Phase 框架）
python3 main.py run --file experiments/tier0/petsite-pod-kill.yaml --dry-run
```

### 4. 查询历史

```bash
python3 main.py history --service petsite --limit 10
```

## 5 Phase 执行流程

| Phase | 名称 | 职责 |
|-------|------|------|
| 0 | Pre-flight | Target Resolver 解析 ARN/Pod → 写审计文件 → 环境检查 |
| 1 | Steady State Before | 采集稳态基线（成功率 + P99），不达标则 abort |
| 2 | Fault Injection | FIS: create_template → start_experiment / ChaosMesh: kubectl apply |
| 3 | Observation | 每 10s 采样，Stop Condition 实时检测，超阈值自动熔断 + RCA 触发 |
| 4 | Recovery | 等待故障到期，轮询 Pod/FIS 状态直到恢复（超时 300s） |
| 5 | Steady State After | 验证恢复稳态 → 判定 PASSED/FAILED → 报告 + DynamoDB + 图谱反馈 |

## 服务名映射

YAML 使用 Neptune 逻辑名，Chaos Mesh 注入时自动映射为 K8s `app=` label：

| YAML service 名 | K8s app label | 说明 |
|-----------------|---------------|------|
| `petsearch` | `search-service` | 搜索服务 |
| `payforadoption` | `pay-for-adoption` | 支付服务 |
| `petlistadoptions` | `list-adoptions` | 领养列表 |
| `petsite` | `petsite` | 主站（一致） |
| `pethistory` | `pethistory` | 领养历史（一致） |
| `petstatusupdater` | N/A（Lambda 函数） | 走 FIS，不走 ChaosMesh |

## 实验模板说明

实验 YAML 只写逻辑名，运行时由 TargetResolver 自动解析真实 ARN：

```yaml
# FIS 实验 — 逻辑名，无硬编码 ARN
extra_params:
  service_name: "statusupdater"     # 逻辑服务名
  resource_type: "lambda:function"  # 资源类型
  # function_arn 运行时自动填充

# Chaos Mesh 实验 — K8s label selector，自动映射
target:
  service: petsearch    # 逻辑名，自动映射为 app=search-service
  namespace: default
```
