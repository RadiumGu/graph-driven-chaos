English | [中文文档](./README.md)

# chaos-automation

AI-driven chaos engineering platform for PetSite microservices — hypothesis generation → 5-phase experiment engine → closed-loop learning, with AWS FIS + Chaos Mesh dual backends.

---

## Ecosystem — Three Projects, One Platform

This repo is the **AI-driven chaos engineering platform** of a larger observability + resilience platform built around PetSite on AWS EKS. Three independent repos work together:

```
┌─────────────────────────────────────────────────────────────────┐
│                     PetSite on AWS EKS                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
         ┌──────────────────▼──────────────────┐
         │  📦 graph-dp-cdk                    │
         │  CDK infra + modular ETL pipeline   │
         │  → builds Neptune knowledge graph   │
         └────┬─────────────────────┬──────────┘
              │ graph queries       │ alarm trigger
              │                     │
   ┌──────────▼──────────┐  ┌──────▼───────────────────┐
   │  🔍 graph-rca-engine │  │  💥 graph-driven-chaos   │
   │  Multi-layer RCA     │  │  (this repo)             │
   │  + Layer2 Probers    │  │  AI-driven chaos         │
   │  + Graph RAG reports │  │  engineering platform    │
   └──────────┬──────────┘  └──────┬───────────────────┘
              │  writes incidents   │  validates RCA
              └────────────────────┘
                    closed loop
```

| Project | Repo | Role |
|---------|------|------|
| **graph-dp-cdk** | [`RadiumGu/graph-dependency-managerment`](https://github.com/RadiumGu/graph-dependency-managerment) | Infrastructure layer — CDK stacks, Neptune ETL pipeline, DeepFlow + AWS topology ingestion |
| **graph-rca-engine** | [`RadiumGu/graph-rca-engine`](https://github.com/RadiumGu/graph-rca-engine) | AIOps RCA engine — multi-layer root cause analysis, plugin-based AWS probers, Bedrock Graph RAG reports |
| **graph-driven-chaos** | [`RadiumGu/graph-driven-chaos`](https://github.com/RadiumGu/graph-driven-chaos) | AI-driven chaos engineering — hypothesis generation, 5-phase experiment runner, closed-loop learning |

**Data flow:** `graph-dp-cdk` ETL populates Neptune → CloudWatch Alarm triggers `graph-rca-engine` → `graph-driven-chaos` injects faults to validate RCA accuracy → results feed back into Neptune.

---

## Architecture

```
                         main.py (CLI entry)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      HypothesisAgent   Orchestrator    LearningAgent
      (Bedrock LLM        (batch runner    (experiment result
       + Neptune graph      sequential/      closed-loop learning
       + TargetResolver     parallel)        + graph update)
         live snapshot)
              │               │               │
              ▼               ▼               ▼
         hypotheses.json  ┌─── ExperimentRunner (5 Phase) ───┐
                          │                                   │
                TargetResolver          Experiment YAML
                (Neptune → AWS API      (logical names,
                 → targets-*.json)       no hardcoded ARNs)
                          │                                   │
                ┌─────────┴─────────┐                         │
                │                   │                         │
          FISClient          ChaosMCPClient                   │
          (AWS FIS API)      (kubectl apply CRD)              │
                │                   │                         │
                ▼                   ▼                         ▼
          targets-fis.json   targets-chaosmesh.json    DeepFlow Metrics
          (audit trail)      (audit trail)             (observability
                                                        + guardrails)
                                                              │
                                                    ┌─────────┴─────────┐
                                                    │                   │
                                              RCA Engine          Reporter
                                              (Lambda)        (Markdown + DDB)
                                                                      │
                                                              CloudWatch Metrics
                                                              (ChaosEngineering)
```

## Prerequisites

### Runtime

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | >= 3.12 | Runtime |
| boto3 | >= 1.34 | AWS API (FIS / RDS / EKS / Lambda / CloudWatch / DynamoDB / S3) |
| structlog | >= 23.0 | Structured logging (JSON format, CloudWatch friendly) |
| PyYAML | >= 6.0 | Experiment YAML parsing |
| requests | >= 2.28 | Neptune HTTP API |
| kubectl | >= 1.28 | Chaos Mesh CRD injection / Pod status queries |
| aws-cli | >= 2.15 | FIS infrastructure setup (`fis_setup.py`) |

### Python dependencies

```bash
pip install boto3 pyyaml requests structlog
```

> All other imports are Python standard library (json / subprocess / logging / ssl / urllib, etc.)

### AWS Resources

| Resource | Description |
|----------|-------------|
| IAM Role | `chaos-fis-experiment-role` — FIS experiment execution role (auto-created by `fis_setup.py`) |
| CloudWatch Alarms | `chaos-*` — 5 FIS stop-condition alarms (auto-created by `fis_setup.py`) |
| S3 Bucket | `chaos-fis-config-{ACCOUNT_ID}` — FIS Lambda Extension communication (auto-created by `fis_setup.py`) |
| DynamoDB Table | `chaos-experiments` — Experiment history (created by `main.py setup`) |
| Neptune Cluster | `petsite-neptune` — Service topology graph (Target Resolver + RCA + Graph Feedback) |
| EKS Cluster | `PetSite` — Target microservice cluster |
| Lambda | `petsite-rca-engine` — RCA root cause analysis engine |
| DeepFlow | Observability platform — success rate / latency metrics |

### DeepFlow Prerequisites

The platform's metric collection (success rate / P99 latency) **depends on** DeepFlow ClickHouse. Ensure the following before running:

| Item | Requirement |
|------|-------------|
| DeepFlow version | >= 6.4 (recommend 6.5+, supports `response_status` field 0=normal) |
| ClickHouse port | 8123 (HTTP), default IP: `11.0.2.30` (override via `DEEPFLOW_CH_HOST`) |
| Required table | `flow_log.l7_flow_log` (L7 flow log table) |

**Quick verification:**

```bash
# Check ClickHouse connectivity
curl -s "http://11.0.2.30:8123/?query=SELECT+1"
# Expected: 1

# Check l7_flow_log table exists
curl -s "http://11.0.2.30:8123/?query=SHOW+TABLES+FROM+flow_log"
```

### Kubernetes

| Dependency | Description |
|------------|-------------|
| Chaos Mesh >= 2.6 | Deployed on EKS cluster, provides PodChaos / NetworkChaos / StressChaos CRDs |
| kubeconfig | Configured with PetSite cluster access |

#### Quick Chaos Mesh deployment

```bash
# Option 1: Use the bundled install script (recommended)
bash /home/ubuntu/tech/chaos/Chaosmesh-MCP/setup-uvx.sh

# Option 2: Manual Helm install
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update
kubectl create namespace chaos-mesh
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace=chaos-mesh \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --version 2.6.3

# Verify
kubectl get pods -n chaos-mesh
```

### Environment Variables (optional, have defaults)

```bash
export AWS_DEFAULT_REGION=ap-northeast-1
export AWS_ACCOUNT_ID=926093770964
export NEPTUNE_HOST=petsite-neptune.cluster-xxx
export FIS_ROLE_ARN=arn:aws:iam::xxx:role/xxx
export FIS_S3_BUCKET=chaos-fis-config-xxx
export BEDROCK_REGION=us-east-1
export BEDROCK_MODEL=us.anthropic.claude-sonnet-4-20250514
```

---

## Project Structure

```
code/
├── main.py                     # CLI entry (run / setup / history / suite / auto / hypothesis / learn)
├── orchestrator.py             # Batch experiment orchestration (sequential/parallel + tag filtering)
├── resolve_targets.py          # Target resolution CLI tool
├── gen_template.py             # Auto-generate experiment templates from Neptune graph
│
├── agents/
│   ├── models.py               # Data models (Hypothesis / LearningReport / ServiceStats)
│   ├── hypothesis_agent.py     # AI hypothesis generation (Bedrock LLM + Neptune graph)
│   └── learning_agent.py       # Closed-loop learning (experiment history analysis + hypothesis iteration)
│
├── runner/
│   ├── config.py               # Centralised config (Region / Account / Neptune / FIS)
│   ├── target_resolver.py      # Runtime ARN resolution (Neptune → AWS API → cache)
│   ├── experiment.py           # Experiment data model + YAML parsing
│   ├── runner.py               # 5-phase execution engine
│   ├── fault_injector.py       # Fault injection abstraction (unified FIS / ChaosMesh interface)
│   ├── fis_backend.py          # AWS FIS backend (create_template → start_experiment)
│   ├── chaos_mcp.py            # Chaos Mesh backend (kubectl apply CRD)
│   ├── metrics.py              # DeepFlow metric collection
│   ├── observability.py        # Structured logging + CloudWatch Metrics publishing
│   ├── rca.py                  # RCA trigger
│   ├── report.py               # Markdown report + DynamoDB write
│   ├── graph_feedback.py       # Neptune graph feedback (chaos_resilience_score)
│   ├── query.py                # DeepFlow query client
│   └── result.py               # ExperimentResult data model
│
├── experiments/
│   ├── fis/                    # AWS FIS experiments (8)
│   │   ├── eks-node/           # EKS node termination
│   │   ├── lambda/             # Lambda latency/error injection
│   │   ├── network-infra/      # Network disruption + EBS IO latency
│   │   └── rds/                # Aurora failover / instance reboot
│   ├── tier0/                  # Chaos Mesh Tier0 experiments (4)
│   ├── tier1/                  # Chaos Mesh Tier1 experiments (3)
│   └── network/                # Chaos Mesh network experiments
│
├── infra/
│   ├── fis_setup.py            # One-click FIS infrastructure setup (IAM + Alarms + S3)
│   └── dynamodb_setup.py       # DynamoDB table creation
│
├── targets-fis.json            # FIS target ARN audit trail (auto-generated)
├── targets-chaosmesh.json      # Chaos Mesh pod target audit trail (auto-generated)
│
└── fmea/
    └── fmea.py                 # FMEA failure mode analysis
```

---

## Quick Start

### 1. Initialise infrastructure

```bash
# Create FIS IAM Role + CloudWatch Alarms + S3 Bucket
cd code/infra && python3 fis_setup.py

# Create DynamoDB experiment history table
cd code && python3 main.py setup
```

### 2. Resolve targets (preview + audit trail)

```bash
cd code

# Resolve all experiment targets (FIS ARN + ChaosMesh Pod)
python3 resolve_targets.py

# FIS only / ChaosMesh only
python3 resolve_targets.py --backend fis
python3 resolve_targets.py --backend chaosmesh

# Force refresh (clear cache)
python3 resolve_targets.py --refresh
```

### 3. Run experiments

```bash
cd code

# Chaos Mesh experiment
python3 main.py run --file experiments/tier0/petsite-pod-kill.yaml

# FIS experiment
python3 main.py run --file experiments/fis/rds/fis-aurora-failover.yaml

# Dry-run (no injection, full 5-phase framework)
python3 main.py run --file experiments/tier0/petsite-pod-kill.yaml --dry-run
```

### 4. AI hypothesis generation & automated experiments

```bash
cd code

# Generate chaos hypotheses (Neptune graph + Bedrock LLM)
python3 main.py hypothesis generate --service petsearch

# List existing hypotheses
python3 main.py hypothesis list

# Export hypotheses as executable experiment YAML
python3 main.py hypothesis export --output experiments/generated/

# End-to-end automation: hypothesis → experiment → analysis
python3 main.py auto --max-hypotheses 5 --top 3 --dry-run

# With tag filtering
python3 main.py auto --max-hypotheses 5 --top 3 --tags env=staging
```

### 5. Batch orchestration

```bash
# Batch execute by directory
python3 main.py suite --dir experiments/tier0/ --strategy by_priority

# Parallel execution + fail-fast
python3 main.py suite --dir experiments/tier1/ --max-parallel 3 --stop-on-failure

# Strategies: by_tier / by_priority / by_domain / full_suite
python3 main.py suite --dir experiments/ --strategy by_domain --top 5
```

### 6. Closed-loop learning

```bash
# Analyse experiment history, generate learning report + update hypothesis library
python3 main.py learn
```

### 7. Query history

```bash
python3 main.py history --service petsite --limit 10
```

---

## 5-Phase Execution Engine

| Phase | Name | Responsibility |
|-------|------|----------------|
| 0 | Pre-flight | Target Resolver resolves ARN/Pod → writes audit file → environment checks |
| 1 | Steady State Before | Collect steady-state baseline (success rate + P99); abort if below threshold |
| 2 | Fault Injection | FIS: create_template → start_experiment / ChaosMesh: kubectl apply |
| 3 | Observation | Sample every 10s, real-time stop-condition detection, auto-circuit-break + RCA trigger |
| 4 | Recovery | Wait for fault expiry, poll Pod/FIS status until recovered (timeout 300s) |
| 5 | Steady State After | Verify recovery → PASSED/FAILED verdict → report + DynamoDB + graph feedback + CloudWatch Metrics |

---

## AI Agents

### HypothesisAgent (Hypothesis Generation)

Generates chaos experiment hypotheses using Neptune service topology + **TargetResolver live infrastructure snapshots** + Bedrock LLM:

1. **Neptune graph** — Extracts service dependencies, tier levels, historical fault events
2. **TargetResolver snapshot** — Gets live Pod count, node distribution, AWS resource ARNs per service
3. **DynamoDB history** — Previously executed experiments (avoids duplication)
4. **Bedrock LLM reasoning** — Combines topology + live state to generate hypotheses, auto-adapts parameters:
   - 2-replica service → `fixed:1` instead of `fixed-percent:50`
   - Single-node deployment → more conservative injection params
   - Has Lambda/RDS resources → also generates FIS experiments
   - No running Pods → skips Chaos Mesh hypotheses
5. Hypothesis scoring + deduplication + export to experiment YAML

### LearningAgent (Closed-Loop Learning)

Automatic experiment result analysis for continuous improvement:

- Reads experiment history from DynamoDB, aggregates by service
- Identifies recurring failure patterns, coverage gaps, trend changes
- LLM generates improvement recommendations + iterates hypothesis library
- Updates Neptune graph (resilience_score / failure_pattern)
- Outputs `learning_report.md`

### Orchestrator (Batch Orchestration)

- Supports sequential / parallel multi-experiment execution
- 4 ordering strategies: `by_tier` / `by_priority` / `by_domain` / `full_suite`
- Inter-experiment cooldown, fail-fast, `--tags` resource filtering

---

## CloudWatch Metrics (Observability)

Each experiment automatically publishes metrics to the CloudWatch `ChaosEngineering` namespace:

### Experiment metrics

| Metric | Unit | Description |
|--------|------|-------------|
| `ExperimentDuration` | Seconds | Total experiment duration |
| `RecoveryTime` | Seconds | Fault recovery time |
| `MinSuccessRate` | Percent | Minimum success rate during experiment |
| `MaxLatencyP99` | Milliseconds | Maximum P99 latency during experiment |
| `DegradationRate` | Percent | Success rate degradation (baseline vs minimum) |
| `ExperimentCount` | Count | Experiment count (+1 each time) |
| `ExperimentPassed` | Count | 1=passed / 0=failed |

**Dimensions:** `Service` / `FaultType` / `Status`

### Phase timing

| Metric | Unit | Description |
|--------|------|-------------|
| `PhaseDuration` | Seconds | Individual phase execution time |

**Dimensions:** `ExperimentId` / `Phase` (phase0–phase5)

---

## Service Name Mapping

YAML uses Neptune logical names; Chaos Mesh injection auto-maps to K8s `app=` labels:

| YAML service name | K8s app label | Notes |
|-------------------|---------------|-------|
| `petsearch` | `search-service` | Search service |
| `payforadoption` | `pay-for-adoption` | Payment service |
| `petlistadoptions` | `list-adoptions` | Adoption listing |
| `petsite` | `petsite` | Main site (same) |
| `pethistory` | `pethistory` | Adoption history (same) |
| `petstatusupdater` | N/A (Lambda function) | Uses FIS, not ChaosMesh |

---

## Chaosmesh-MCP

`Chaosmesh-MCP/` is a standalone [FastMCP](https://github.com/jlowin/fastmcp) server **for interactive Chaos Mesh exploration by AI agents** (e.g., Amazon Q). It is NOT the main execution path.

| Component | Purpose | Execution path |
|-----------|---------|----------------|
| `chaos_mcp.py` (`ChaosMCPClient`) | **Main path** — `kubectl apply CRD` direct injection | `runner.py` → `ChaosMCPClient` |
| `Chaosmesh-MCP/server.py` | AI agent exploration interface (MCP protocol) | Amazon Q / AI Agent → MCP Server |

> Production experiments never go through the MCP Server. It is a helper tool for AI tools to interactively query/operate Chaos Mesh resources.

---

## Experiment Templates

Experiment YAML uses logical names only; TargetResolver auto-resolves real ARNs at runtime:

```yaml
# FIS experiment — logical names, no hardcoded ARNs
extra_params:
  service_name: "statusupdater"     # Logical service name
  resource_type: "lambda:function"  # Resource type
  # function_arn auto-populated at runtime

# Chaos Mesh experiment — K8s label selector, auto-mapped
target:
  service: petsearch    # Logical name, auto-mapped to app=search-service
  namespace: default
```
