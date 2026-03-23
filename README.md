[English](./README_EN.md) | 中文文档

# chaos-automation

PetSite 微服务混沌工程自动化平台 — AI 驱动的假设生成 → 5 Phase 实验引擎 → 闭环学习，支持 AWS FIS + Chaos Mesh 双后端。

---

## 生态全景 — 三个项目，一个平台

本仓库是基于 PetSite (AWS EKS) 构建的可观测性 + 弹性验证平台中的 **AI 驱动混沌工程平台**。三个独立仓库协同工作：

```
┌─────────────────────────────────────────────────────────────────┐
│                     PetSite on AWS EKS                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
         ┌──────────────────▼──────────────────┐
         │  📦 graph-dp-cdk                    │
         │  CDK 基础设施 + 模块化 ETL 管道      │
         │  → 构建 Neptune 知识图谱             │
         └────┬─────────────────────┬──────────┘
              │ 图谱查询            │ 告警触发
              │                     │
   ┌──────────▼──────────┐  ┌──────▼───────────────────┐
   │  🔍 graph-rca-engine │  │  💥 graph-driven-chaos   │
   │  多层 RCA 分析        │  │  （本仓库）              │
   │  + Layer2 探针        │  │  AI 驱动的混沌工程平台   │
   │  + Graph RAG 报告     │  │  (Chaos Mesh + AWS FIS)  │
   └──────────┬──────────┘  └──────┬───────────────────┘
              │  写入事件记录       │  验证 RCA 准确性
              └────────────────────┘
                      闭环
```

| 项目 | 仓库 | 定位 |
|------|------|------|
| **graph-dp-cdk** | [`RadiumGu/graph-dependency-managerment`](https://github.com/RadiumGu/graph-dependency-managerment) | 基础设施层 — CDK 栈、Neptune ETL 管道、DeepFlow + AWS 拓扑采集 |
| **graph-rca-engine** | [`RadiumGu/graph-rca-engine`](https://github.com/RadiumGu/graph-rca-engine) | AIOps 根因分析引擎 — 多层根因分析、插件化 AWS 探针、Bedrock Graph RAG 报告 |
| **graph-driven-chaos** | [`RadiumGu/graph-driven-chaos`](https://github.com/RadiumGu/graph-driven-chaos) | AI 驱动的混沌工程 — 假设生成、5 阶段实验引擎、闭环学习 |

**数据流：** `graph-dp-cdk` ETL 填充 Neptune → CloudWatch 告警触发 `graph-rca-engine` → `graph-driven-chaos` 注入故障验证 RCA 准确性 → 结果回写 Neptune。

---

## 架构概览

```
                         main.py (CLI 入口)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      HypothesisAgent   Orchestrator    LearningAgent
      (Bedrock LLM        (批量编排       (实验结果闭环
       + Neptune 图谱       顺序/并行)      学习 + 图谱更新)
       + TargetResolver
         实时快照)
              │               │               │
              ▼               ▼               ▼
         hypotheses.json  ┌─── ExperimentRunner (5 Phase) ───┐
                          │                                   │
                TargetResolver          Experiment YAML
                (Neptune → AWS API      (逻辑名，无硬编码 ARN)
                 → targets-*.json)
                          │                                   │
                ┌─────────┴─────────┐                         │
                │                   │                         │
          FISClient          ChaosMCPClient                   │
          (AWS FIS API)      (kubectl apply CRD)              │
                │                   │                         │
                ▼                   ▼                         ▼
          targets-fis.json   targets-chaosmesh.json    DeepFlow Metrics
          (审计留底)          (审计留底)                (观测 + Guardrails)
                                                              │
                                                    ┌─────────┴─────────┐
                                                    │                   │
                                              RCA Engine          Reporter
                                              (Lambda)        (Markdown + DDB)
                                                                      │
                                                              CloudWatch Metrics
                                                              (ChaosEngineering)
```

## 环境依赖

### 运行时

| 依赖 | 版本要求 | 用途 |
|------|---------|------|
| Python | >= 3.12 | 运行时 |
| boto3 | >= 1.34 | AWS API（FIS / RDS / EKS / Lambda / CloudWatch / DynamoDB / S3） |
| structlog | >= 23.0 | 结构化日志（JSON 格式，CloudWatch 友好） |
| PyYAML | >= 6.0 | 实验 YAML 解析 |
| requests | >= 2.28 | Neptune HTTP API |
| kubectl | >= 1.28 | Chaos Mesh CRD 注入 / Pod 状态查询 |
| aws-cli | >= 2.15 | FIS 基础设施配置脚本（`fis_setup.py`） |

### Python 依赖安装

```bash
pip install boto3 pyyaml requests structlog
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

### DeepFlow 先决条件

本平台的指标采集（成功率 / P99 延迟）**强依赖** DeepFlow ClickHouse，运行前须确保以下条件满足：

| 项目 | 要求 |
|------|------|
| DeepFlow 版本 | >= 6.4（建议 6.5+，支持 `response_status` 字段 0=正常） |
| ClickHouse 端口 | 8123（HTTP），默认 IP：`11.0.2.30`（可通过 `DEEPFLOW_CH_HOST` 覆盖） |
| 必需表 | `flow_log.l7_flow_log`（L7 七层流日志表） |

**flow_log.l7_flow_log 最小 schema（必需字段）：**

```sql
-- 验证表存在且字段可用
SELECT
    response_status,        -- 0=正常, >=1=异常
    response_duration,      -- 微秒
    request_domain,         -- svc-name.namespace.svc.cluster.local
    start_time
FROM flow_log.l7_flow_log
LIMIT 1
```

**DeepFlow 快速验证：**

```bash
# 检查 ClickHouse 连通性
curl -s "http://11.0.2.30:8123/?query=SELECT+1"
# 预期返回: 1

# 检查 l7_flow_log 表存在
curl -s "http://11.0.2.30:8123/?query=SHOW+TABLES+FROM+flow_log"
```

> **TODO（生产环境）：** DeepFlow ClickHouse 通常需要用户名/密码认证。当前实现无认证，生产部署时应在 `metrics.py` 的 `_ch_query()` 中加入 `X-ClickHouse-User` / `X-ClickHouse-Key` Header（参见 ClickHouse HTTP 接口文档）。

### Kubernetes 环境

| 依赖 | 说明 |
|------|------|
| Chaos Mesh | >= 2.6 | 部署在 EKS 集群，提供 PodChaos / NetworkChaos / StressChaos 等 CRD |
| kubeconfig | 已配置 PetSite 集群访问权限 |

#### Chaos Mesh 快速部署

```bash
# 方式一：使用项目自带安装脚本（推荐）
# 脚本位于 Chaosmesh-MCP/setup-uvx.sh，自动安装 Chaos Mesh 并配置 MCP Server
bash /home/ubuntu/tech/chaos/Chaosmesh-MCP/setup-uvx.sh

# 方式二：Helm 手动安装
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update
kubectl create namespace chaos-mesh
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace=chaos-mesh \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --version 2.6.3

# 验证安装
kubectl get pods -n chaos-mesh
```

> **注意：** `Chaosmesh-MCP/` 是一个独立的 MCP Server，**供 Amazon Q / AI Agent 交互式探索使用**，
> 并非本平台主执行路径（主路径通过 `kubectl apply CRD` 注入，见 `chaos_mcp.py`）。详见下方说明。

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
├── main.py                     # CLI 入口（run / setup / history / suite / auto / hypothesis / learn）
├── orchestrator.py             # 批量实验编排（顺序/并行执行 + tag 过滤）
├── resolve_targets.py          # 目标解析 CLI 工具
├── gen_template.py             # 从 Neptune 图谱自动生成实验模板
│
├── agents/
│   ├── models.py               # 数据模型（Hypothesis / LearningReport / ServiceStats 等）
│   ├── hypothesis_agent.py     # AI 假设生成 Agent（Bedrock LLM + Neptune 图谱）
│   └── learning_agent.py       # 闭环学习 Agent（实验历史分析 + 假设迭代）
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
│   ├── observability.py        # 结构化日志 + CloudWatch Metrics 发布
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
# --backend 默认值: "all"（同时解析 FIS + ChaosMesh 两类目标）
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

### 4. AI 假设生成 & 自动化实验

```bash
cd code

# 生成混沌实验假设（基于 Neptune 图谱 + Bedrock LLM）
python3 main.py hypothesis generate --service petsearch

# 列出现有假设
python3 main.py hypothesis list

# 导出假设为可执行的实验 YAML
python3 main.py hypothesis export --output experiments/generated/

# 端到端自动化：假设生成 → 实验执行 → 结果分析
python3 main.py auto --max-hypotheses 5 --top 3 --dry-run

# 带 tag 过滤（只对特定标签的资源执行）
python3 main.py auto --max-hypotheses 5 --top 3 --tags env=staging
```

### 5. 批量编排

```bash
# 按目录批量执行实验
python3 main.py suite --dir experiments/tier0/ --strategy by_priority

# 并行执行 + 失败即停
python3 main.py suite --dir experiments/tier1/ --max-parallel 3 --stop-on-failure

# 策略选项: by_tier / by_priority / by_domain / full_suite
python3 main.py suite --dir experiments/ --strategy by_domain --top 5
```

### 6. 闭环学习

```bash
# 分析实验历史，生成学习报告 + 更新假设库
python3 main.py learn
```

### 7. 查询历史

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
| 5 | Steady State After | 验证恢复稳态 → 判定 PASSED/FAILED → 报告 + DynamoDB + 图谱反馈 + CloudWatch Metrics |

## AI Agents

### Hypothesis Agent（假设生成）

基于 Neptune 服务拓扑图谱 + **TargetResolver 实时基础设施快照** + Bedrock LLM，自动生成混沌实验假设：

1. **Neptune 图谱** — 提取服务依赖关系、Tier 等级、历史故障事件
2. **TargetResolver 快照** — 获取每个服务的实时 Pod 数量、节点分布、AWS 资源 ARN
3. **DynamoDB 历史** — 已执行过的实验结果（避免重复）
4. **Bedrock LLM 推理** — 结合拓扑 + 实时状态生成假设，自动适配实验参数：
   - 2 副本服务 → `fixed:1` 而非 `fixed-percent:50`
   - 单节点部署 → 更保守的注入参数
   - 有 Lambda/RDS 资源 → 同时生成 FIS 实验
   - 无 running Pod → 跳过 Chaos Mesh 类假设
5. 假设评分 + 去重 + 导出为实验 YAML

### Learning Agent（闭环学习）

实验结果自动分析，持续改进：

- 从 DynamoDB 读取实验历史，按服务聚合统计
- 识别重复失败模式、覆盖盲区、趋势变化
- LLM 生成改进建议 + 迭代更新假设库
- 更新 Neptune 图谱（resilience_score / failure_pattern）
- 输出 `learning_report.md`

### Orchestrator（批量编排）

- 支持顺序 / 并行执行多个实验
- 4 种排序策略：`by_tier` / `by_priority` / `by_domain` / `full_suite`
- 实验间冷却时间、失败即停、`--tags` 资源过滤

## CloudWatch Metrics（可观测性）

每次实验自动发布指标到 CloudWatch `ChaosEngineering` namespace：

### 实验指标

| 指标名 | 单位 | 说明 |
|--------|------|------|
| `ExperimentDuration` | Seconds | 实验总耗时 |
| `RecoveryTime` | Seconds | 故障恢复时间 |
| `MinSuccessRate` | Percent | 实验期间最低成功率 |
| `MaxLatencyP99` | Milliseconds | 实验期间最高 P99 延迟 |
| `DegradationRate` | Percent | 成功率下降幅度（基线 vs 最低） |
| `ExperimentCount` | Count | 实验计数（每次 +1） |
| `ExperimentPassed` | Count | 1=通过 / 0=失败 |

**Dimensions:** `Service` / `FaultType` / `Status`

### Phase 耗时

| 指标名 | 单位 | 说明 |
|--------|------|------|
| `PhaseDuration` | Seconds | 单个 Phase 执行耗时 |

**Dimensions:** `ExperimentId` / `Phase`（phase0-phase5）

> 可在 CloudWatch Console 创建 Dashboard 监控实验趋势，或设置 Alarm 对恢复时间/成功率做告警。

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

## Chaosmesh-MCP 定位说明

`Chaosmesh-MCP/` 是一个独立的 [FastMCP](https://github.com/jlowin/fastmcp) Server，
**供 Amazon Q / AI Agent 交互式探索 Chaos Mesh 资源时使用**，不是本平台的主执行路径。

| 组件 | 用途 | 执行路径 |
|------|------|---------|
| `chaos_mcp.py` (`ChaosMCPClient`) | **主执行路径** — `kubectl apply CRD` 直接注入 | `runner.py` → `ChaosMCPClient` |
| `Chaosmesh-MCP/server.py` | AI Agent 探索接口（MCP 协议） | Amazon Q / AI Agent → MCP Server |

> **结论：** 生产实验执行完全不经过 MCP Server。Chaosmesh-MCP 是给 AI 工具（如 Amazon Q）
> 做交互式查询/操作用的辅助工具，可独立部署，对主实验引擎无依赖。

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
