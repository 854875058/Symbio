<div align="center">

# 🧬 SYMBIO

### The Next-Gen AI Infrastructure for Multi-Agent Orchestration

---

**English** | [中文](README_zh.md) | [日本語](README_ja.md)

---

**From a simple "Agent wrapper" to a self-evolving, enterprise-grade AI Infrastructure**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Stars](https://img.shields.io/github/stars/854875058/Symbio?style=social)](https://github.com/854875058/Symbio)
[![GitHub Forks](https://img.shields.io/github/forks/854875058/Symbio?style=social)](https://github.com/854875058/Symbio)

</div>

---

## Why Symbio?

<table>
<tr>
<td width="50%">

### The Problem

- 🤖 Agent frameworks are just LLM wrappers
- 🧠 Memory is just vector search
- ⏰ Agents declare completion prematurely
- 💬 Communication costs explode exponentially
- 🔒 Security is an afterthought
- 📊 No observability, pure black box

</td>
<td width="50%">

### The Symbio Solution

- ⚡ Dynamic DAG with runtime topology evolution
- 🧬 Ontology-powered cognitive memory graph
- 🛡️ Anti-premature completion with TDD loop
- 📉 State-driven communication (-80% tokens)
- 🔐 Neuro-symbolic security firewall
- 👁️ Full OpenTelemetry observability

</td>
</tr>
</table>

---

## 33 Killer Features

<details>
<summary><b>🧠 Core Engine</b></summary>

| Feature | Description |
|---------|-------------|
| ⚡ Dynamic DAG | Runtime topology evolution - "No fixed strategy, adapt like water" |
| 🎯 Smart Routing | User-configurable model pool with Pareto-optimal routing |
| ✂️ Context Pruning | Semantic-level compression with Prompt Cache alignment |
| 🛡️ Anti-Premature | Forced Tool Calling + Test-Driven Verification Loop |

</details>

<details>
<summary><b>👥 Multi-Agent Collaboration</b></summary>

| Feature | Description |
|---------|-------------|
| 🔄 SubAgent Dispatch | Ray-Native distributed Actor runtime |
| ⚖️ Consensus Debate | Hegelian dialectic: Proposer + Critic + Refiner |
| 📨 State-Driven Comm | Global state object, zero dialogue passing |

</details>

<details>
<summary><b>💾 Cognitive Memory</b></summary>

| Feature | Description |
|---------|-------------|
| 🧬 Ontology Memory | T-Box/A-Box separated neuro-symbolic cognitive graph |
| 💰 Semantic Cache | Similar requests reuse results, 0 token cost |
| 🏠 Project Isolation | Each project is an independent "memory universe" |

</details>

<details>
<summary><b>🛠️ Tools & Security</b></summary>

| Feature | Description |
|---------|-------------|
| 🔌 MCP Native | Standardized tool mounting, plug-and-play |
| 📦 Absolute Sandbox | Container/VM physical isolation |
| 🛡️ Injection Guard | 3-layer defense, 0ms hard intercept |

</details>

<details>
<summary><b>🚀 Evolution & Intelligence</b></summary>

| Feature | Description |
|---------|-------------|
| 🔄 Data Flywheel | Trajectory capture → Fine-tuning dataset export |
| 🧠 Self-Evolution | Prompt performance tracking + auto-optimization |
| 📊 Eval Pipeline | Automated regression detection |

</details>

<details>
<summary><b>🌐 Interface & Protocols</b></summary>

| Feature | Description |
|---------|-------------|
| 👤 HITL | IM async approval, one-click mobile authorization |
| 🤝 A2A Protocol | Interoperate with external agents |
| 🖥️ Computer Use | Screenshot → Vision → GUI Control |
| 🎨 Multi-Modal | Image/Document/Audio unified processing |

</details>

<details>
<summary><b>📊 Observability</b></summary>

| Feature | Description |
|---------|-------------|
| 🔍 OpenTelemetry | Full-chain Trace visualization |
| 🔥 Token Heatmap | Real-time cost monitoring |
| ⏸️ Memory Snapshot | Breakpoint recovery for debugging |

</details>

<details>
<summary><b>🔒 Enterprise Features</b></summary>

| Feature | Description |
|---------|-------------|
| 🔐 Privacy Computing | Federated Learning + Differential Privacy |
| 📱 Edge Computing | Cloud-Edge-Device layered deployment |
| 🔄 Version Compat | Seamless smooth upgrade |
| 📝 PromptOps | Prompt versioning + A/B testing |

</details>

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      🌐 Interface Layer                         │
│      CLI  ·  Web UI  ·  Desktop  ·  IM (QQ/WeChat/Feishu)      │
├─────────────────────────────────────────────────────────────────┤
│                      🧠 Orchestrator Layer                       │
│      Dynamic DAG  ·  Smart Routing  ·  Security Gateway         │
├─────────────────────────────────────────────────────────────────┤
│                      👥 Agent Layer                              │
│      Main Agent  ·  SubAgent  ·  Consensus Debate  ·  Simulator │
├─────────────────────────────────────────────────────────────────┤
│                      💾 Foundation Layer                         │
│      Tools  ·  Memory  ·  Evolution  ·  Config  ·  Security     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
# Install
pip install symbio

# Initialize project
symbio init

# Start services
symbio start

# Open Web UI
open http://localhost:9090
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [Feature List](docs/features.md) | 33 killer features detailed definition |
| [Architecture](docs/architecture.md) | 4-layer architecture + security/observability |
| [Module Whitepaper](docs/module-design-whitepaper.md) | 17 modules with forward-looking design |
| [UI Design](docs/ui-design.md) | 28 pages + component system + interactions |
| [Roadmap](docs/roadmap.md) | 10 Phases complete development plan |
| [Module Plan](docs/modules.md) | Module tree + code skeleton |
| [Tech Stack](docs/tech-stack.md) | Technology selection + dependencies |
| [References](docs/references.md) | Competitor analysis + reference projects |

---

## Tech Stack

| Layer | Selection |
|-------|-----------|
| Core | Python 3.10+ · asyncio · uvloop |
| Agent | Custom Dynamic DAG · Ray (optional) |
| Memory | LanceDB · NetworkX · Ontology Reasoning |
| Tools | MCP · Claude Code · Shell · Git |
| Frontend | Next.js 15 · shadcn/ui · Zustand |
| Observability | OpenTelemetry · Jaeger · Grafana |
| Storage | aiosqlite · LanceDB · Redis (optional) |
| Deployment | Docker · K8s (optional) · Tauri |

---

## Roadmap

| Phase | Priority | Deliverables |
|-------|----------|--------------|
| Phase 1 Core | **P0** | Dynamic DAG + 3 Defense Gateways + CLI |
| Phase 2 Multi-Agent | **P0** | SubAgent Dispatch + Consensus Debate |
| Phase 3 Memory | **P1** | LanceDB + Ontology Reasoning Graph |
| Phase 4 Tools | **P1** | MCP + Claude Code + Sandbox |
| Phase 5 Interface | **P2** | IM + HITL + WebUI |
| Phase 6 Evolution | **P2** | Data Flywheel + Eval Pipeline |
| Phase 7 Security | **P2** | Injection Guard + Semantic Cache + Multi-Modal |
| Phase 8 Advanced | **P3** | Cutting-Edge Protocols + Privacy + Edge |

---

## Contributing

We welcome contributions! Please read [Contributing Guide](CONTRIBUTING.md).

---

## License

MIT License - Free to use, modify, and distribute.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=854875058/Symbio&type=Date)](https://star-history.com/#854875058/Symbio&Date)

---

<div align="center">

**⭐ Star us on GitHub — it helps!**

**Symbio — Don't let AI Agent be a wrapper tool**

*Think Big, Start Small.*

</div>
