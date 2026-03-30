# REFACTOR.md — リファクタリング計画

> 作成日: 2026-03-30  
> 対象: `logosyncs` プロジェクト（コアが一通り実装完了した段階）

---

## 現状サマリー

| ファイル | 行数 | 評価 |
|---|---|---|
| `cli.py` | 790 | 🔴 最大・要分割 |
| `search.py` | 380 | 🟡 やや大きい |
| `distiller.py` | 374 | 🟡 やや大きい |
| `resolver.py` | 301 | 🟡 言語追加で肥大リスク |
| `indexer.py` | 213 | 🟢 適切 |
| `embedder.py` | 168 | 🟢 適切 |
| `embedder_server.py` | 165 | 🟢 適切 |
| `db.py` | 155 | 🟢 適切 |

**エラー件数**: `cli.py` 6件 / `indexer.py` 2件 / `embedder_server.py` 2件  
**テスト総数**: 84件（全通過）

---

## Phase A: 型エラー修正（即時・低リスク）

**対象**: `cli.py`（6件）・`indexer.py`（2件）

**内容**:
- `dict` / `list` への型引数追加（`dict[str, Any]`・`list[dict[str, Any]]`）
- `cli.py` L482/L485 の `str` に `.append` が発生している箇所の修正
- `indexer.py` の JSON パース結果の型注釈整備

**方針**: テストを壊さず機械的に修正できるため最初に消化する。

---

## Phase B: `cli.py` 分割（最優先・最大効果）

### 問題

`cli.py`（790行）に以下の責務が混在している:

- コマンドルーティング（typer app 定義）
- DB クエリ（`show`・`dump`・`status` が直接 SQL を発行）
- パス解決ヘルパー（`_find_project_root`・`_resolve_claude_projects_path` 等）
- フック設定（`hook_install` 117行の JSON 書き換えロジック）
- embedding サーバー管理（`server_start/stop/status`）

### 目標構成

```
src/logo/
├── cli.py              # typer app 定義・サブコマンド登録のみ
├── paths.py            # パス解決ヘルパー（_find_project_root 等）
└── commands/
    ├── __init__.py
    ├── index.py        # logo index
    ├── search_cmd.py   # logo search / context
    ├── distill_cmd.py  # logo distill
    ├── server_cmd.py   # logo server start/stop/status
    ├── hook_cmd.py     # logo hook install
    └── show_cmd.py     # logo show / dump / status
```

### 手順

1. `paths.py` を新規作成してパス解決ヘルパーを移動
2. `commands/` ディレクトリを作成
3. コマンドを1つずつ移動（都度 `pytest` でグリーン確認）
4. `cli.py` をルーティングのみにスリム化

---

## Phase C: `distiller.py` 責務分離（中優先）

### 問題

`distiller.py`（374行）に以下が混在:

- LLM 呼び出し（`call_claude`・プロンプトテンプレート・JSON スキーマ）
- 副作用管理（`_cleanup_side_effect_jsonls`・`distill.lock`）
- DB 永続化（`save_palace_object`）
- シンボル解決連携（`SymbolResolver` 呼び出し）
- バッチ処理ループ（`distill_all`・`distill_exchange`）

### 目標構成

```
src/logo/
├── distiller.py        # distill_all / distill_exchange（バッチループのみ）
├── llm.py              # call_claude + プロンプト定数 + JSON スキーマ
└── palace_repo.py      # save_palace_object（DB 永続化・シンボル連携）
```

---

## Phase D: `search.py` データクラス分離（低優先）

### 問題

データクラス（`SearchResult` / `BM25Result` / `HNSWPalaceResult` / `FusedResult`）と検索ロジックが同居している。

### 目標構成

```
src/logo/
├── models.py           # 全データクラス定義
└── search.py           # 検索ロジックのみ（models をインポート）
```

---

## Phase E: `resolver.py` 言語別分割（将来対応）

### 問題

`_walk_python` / `_walk_typescript` / `_walk_go` が `SymbolResolver` 1クラスに集中。言語追加のたびに肥大化する構造。

### 目標構成（言語追加時に対応）

```
src/logo/
└── resolvers/
    ├── __init__.py     # SymbolResolver（ファサード）
    ├── python.py
    ├── typescript.py
    └── go.py
```

**現時点では不要。言語を追加するタイミングで対応する。**

---

## テスト補完（各 Phase と並行）

現在テストが存在しないコマンド:

| コマンド | テストファイル | 優先度 |
|---|---|---|
| `logo index` | なし | 🟡 中 |
| `logo search` | なし（CLI レベル） | 🟡 中 |
| `logo context` | なし | 🟡 中 |
| `logo server start/stop/status` | なし | 🟢 低 |

Phase B の分割と合わせて `tests/commands/` にコマンド単位のテストを追加する。

---

## 優先度まとめ

| Phase | 対象 | 効果 | リスク | 推奨タイミング |
|---|---|---|---|---|
| A | 型エラー修正 | 🟢 小 | 🟢 低 | 今すぐ |
| B | `cli.py` 分割 | 🔴 大 | 🟡 中 | Phase A 完了後 |
| C | `distiller.py` 分離 | 🟡 中 | 🟡 中 | Phase B 完了後 |
| D | `search.py` モデル分離 | 🟢 小 | 🟢 低 | Phase C 完了後 |
| E | `resolver.py` 言語別分割 | 🟢 小 | 🟢 低 | 言語追加時 |