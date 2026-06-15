<div align="center">

# 🧬 SYMBIO（共生）

### 次世代 AI インフラストラクチャ · マルチエージェント協調フレームワーク

**「LLM ラッパーツール」から「自己進化能力を持つエンタープライズ級 AI インフラ」へ昇華**

[English](README_en.md) | [中文](README_zh.md) | **日本語**

---

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Downloads](https://static.pepy.tech/badge/symbio)](https://pepy.tech/project/symbio)
[![GitHub Stars](https://img.shields.io/github/stars/854875058/Symbio?style=social)](https://github.com/854875058/Symbio)

</div>

---

## なぜ Symbio を選ぶのか？

<table>
<tr>
<td width="50%">

### 業界の課題

- 🤖 エージェントフレームワークは LLM のラッパー
- 🧠 メモリシステムはベクトル検索のみ
- ⏰ エージェントが早期に完了を宣言
- 💬 通信コストが指数関数的に爆発
- 🔒 セキュリティは後付け
- 📊 完全なブラックボックス

</td>
<td width="50%">

### Symbio のソリューション

- ⚡ 動的 DAG ランタイムトポロジー進化
- 🧬 オントロジー駆動認知メモリグラフ
- 🛡️ 早期完了防止 + TDD ループ
- 📉 状態駆動通信（-80% トークン）
- 🔐 神経記号セキュリティファイアウォール
- 👁️ フル OpenTelemetry 可観測性

</td>
</tr>
</table>

---

## 主要機能（33 のキラー機能）

<details>
<summary><b>🧠 コアエンジン</b></summary>

| 機能 | 説明 |
|------|------|
| ⚡ 動的 DAG | ランタイムトポロジー進化 |
| 🎯 スマートルーティング | ユーザー設定可能なモデルプール |
| ✂️ コンテキストプルーニング | セマンティックレベル圧縮 |
| 🛡️ 早期完了防止 | 強制 Tool Calling + テスト検証ループ |

</details>

<details>
<summary><b>👥 マルチエージェント協調</b></summary>

| 機能 | 説明 |
|------|------|
| 🔄 SubAgent ディスパッチ | Ray-Native 分散 Actor ランタイム |
| ⚖️ コンセンサスディベート | ヘーゲル弁証法システム |
| 📨 状態駆動通信 | グローバル状態オブジェクト |

</details>

<details>
<summary><b>💾 認知メモリ</b></summary>

| 機能 | 説明 |
|------|------|
| 🧬 オントロジーメモリ | T-Box/A-Box 分離神経記号グラフ |
| 💰 セマンティックキャッシュ | 類似リクエスト結果再利用 |
| 🏠 プロジェクト分離 | 各プロジェクト独立の「メモリユニバース」 |

</details>

<details>
<summary><b>🛠️ ツールとセキュリティ</b></summary>

| 機能 | 説明 |
|------|------|
| 🔌 MCP ネイティブ | 標準化ツールマウント |
| 📦 絶対サンドボックス | コンテナ/VM 物理隔離 |
| 🛡️ Injection 防護 | 3 層防御体系 |

</details>

<details>
<summary><b>🚀 進化と知能</b></summary>

| 機能 | 説明 |
|------|------|
| 🔄 データフライホイール | 軌跡キャプチャ → 微調整データセット自動エクスポート |
| 🧠 自己進化 | プロンプト効果追跡 + 自動最適化 |
| 📊 評価パイプライン | 自動回帰検出 |

</details>

<details>
<summary><b>🌐 インターフェースとプロトコル</b></summary>

| 機能 | 説明 |
|------|------|
| 👤 HITL | IM 非同期承認、ワンクリックモバイル認証 |
| 🤝 A2A プロトコル | 外部エージェントとの相互運用 |
| 🖥️ Computer Use | スクリーンショット → 視覚理解 → GUI 制御 |
| 🎨 マルチモーダル | 画像/ドキュメント/音声統合処理 |

</details>

<details>
<summary><b>📊 可観測性</b></summary>

| 機能 | 説明 |
|------|------|
| 🔍 OpenTelemetry | フルチェーン Trace 可視化 |
| 🔥 トークンヒートマップ | リアルタイムコスト監視 |
| ⏸️ メモリスナップショット | ブレークポイント回復 |

</details>

<details>
<summary><b>🔒 エンタープライズ機能</b></summary>

| 機能 | 説明 |
|------|------|
| 🔐 プライバシー計算 | 連合学習 + 差分プライバシー |
| 📱 エッジコンピューティング | クラウド-エッジ-デバイス階層配置 |
| 🔄 バージョン互換 | シームレスなスムーズアップグレード |
| 📝 PromptOps | プロンプトバージョニング + A/B テスト |

</details>

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│                      🌐 インターフェース層                        │
│      CLI  ·  Web UI  ·  Desktop  ·  IM (QQ/WeChat/Feishu)      │
├─────────────────────────────────────────────────────────────────┤
│                      🧠 オーケストレーター層                      │
│      動的 DAG  ·  スマートルーティング  ·  セキュリティゲートウェイ  │
├─────────────────────────────────────────────────────────────────┤
│                      👥 エージェント層                            │
│      メインエージェント  ·  SubAgent  ·  コンセンサスディベート    │
├─────────────────────────────────────────────────────────────────┤
│                      💾 基盤層                                   │
│      ツール  ·  メモリ  ·  進化エンジン  ·  設定  ·  セキュリティ  │
└─────────────────────────────────────────────────────────────────┘
```

---

## クイックスタート

```bash
# インストール
pip install symbio

# プロジェクト初期化
symbio init

# サービス起動
symbio start

# Web UI を開く
open http://localhost:9090
```

---

## ドキュメント

| ドキュメント | 説明 |
|-------------|------|
| [機能一覧](docs/features.md) | 33 のキラー機能詳細定義 |
| [アーキテクチャ](docs/architecture.md) | 4 層アーキテクチャ |
| [モジュール設計白書](docs/module-design-whitepaper.md) | 17 モジュールの先進設計 |
| [UI 設計](docs/ui-design.md) | 28 ページ + コンポーネントシステム |
| [ロードマップ](docs/roadmap.md) | 10 Phase 開発計画 |

---

## テックスタック

| レイヤー | 選択 |
|---------|------|
| コア | Python 3.10+ · asyncio · uvloop |
| エージェント | カスタム動的 DAG · Ray (オプション) |
| メモリ | LanceDB · NetworkX · オントロジー推論 |
| ツール | MCP · Claude Code · Shell · Git |
| フロントエンド | Next.js 15 · shadcn/ui · Zustand |
| 可観測性 | OpenTelemetry · Jaeger · Grafana |

---

## ロードマップ

| Phase | 優先度 | 成果物 |
|-------|--------|--------|
| Phase 1 コア | **P0** | 動的 DAG + 3 防御ゲートウェイ + CLI |
| Phase 2 マルチエージェント | **P0** | SubAgent + コンセンサスディベート |
| Phase 3 メモリ | **P1** | LanceDB + オントロジー推論グラフ |
| Phase 4 ツール | **P1** | MCP + Claude Code + サンドボックス |
| Phase 5 インターフェース | **P2** | IM + HITL + WebUI |
| Phase 6 進化エンジン | **P2** | データフライホイール + 評価パイプライン |
| Phase 7 セキュリティ | **P2** | Injection 防護 + セマンティックキャッシュ |
| Phase 8 高度機能 | **P3** | 最先端プロトコル + プライバシー + エッジ |

---

## 貢献

貢献を歓迎します！[貢献ガイド](CONTRIBUTING.md)をお読みください。

---

## ライセンス

MIT License - 自由に使用、変更、配布できます。

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=854875058/Symbio&type=Date)](https://star-history.com/#854875058/Symbio&Date)

---

<div align="center">

**⭐ GitHub でスターをつけましょう — それが助けになります！**

**Symbio — AI Agent をラッパーツールにしない**

*大所着眼、小所着手。Think Big, Start Small.*

</div>
