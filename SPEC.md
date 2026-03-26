# SPEC: logosyncs

> コードシンボルと会話履歴をつなぐ、CLI ファーストの記憶層
> コマンド名: `logo`

---

## 1. 概要

CLI 上で動く LLM（Claude Code 等）が過去の会話を検索・参照できるツール。
**主ユーザーはエージェント**（人間ではなく Claude Code 等の AI エージェントが直接呼び出す）。

### 解決したいこと

- エージェントに会話の長期記憶を持たせたい
  - 過去の実装内容・仕様・意思決定・編集ファイルを記憶
- 記憶に対してセマンティック検索をかけたい
  - 「この実装はどんな仕様で実装された？」
  - 「〜関連の機能ってどこに実装してたっけ？」
  - 「〜は実装済みだっけ？」
- 検索結果としてコードの位置（ファイル・行・シンボル）まで返したい
  - コーディングエージェントにとって grep 検索は時間とトークンを食う
  - セマンティック検索 + シンボル解決で高速にコードの位置を特定する

searchat のセマンティック検索に「シンボルベースのコード位置特定」を加え、
**ファイル名レベルではなくシンボルレベルで会話とコードを紐づける**。

---

## 2. 解決する問題

### LLM は毎セッション記憶がゼロになる

```
【3週間前】
自分: AuthMiddleware の token 検証が失敗する
LLM:  validate() に None チェックを追加すれば直る → 解決

【今日】
自分: また同じエラーが出た
LLM:  わかりません（覚えていない）
```

### searchat で解決しきれないこと

searchat は**人間がブラウザで会話を読むツール**。エージェントが使う想定ではない。

| 問題 | searchat | logosyncs |
|------|---------|-----------|
| 主ユーザー | 人間（WebUI） | **エージェント（CLI）** |
| 「この関数について過去に話した？」 | できない（コード→会話の逆引き不可） | できる |
| リネーム・移動への追従 | ファイル名が変わると追跡不能 | ファイル移動に強い（シンボル名で管理）<br>※リネーム前の記録はセマンティック検索でカバー |
| エージェントから呼びやすいか | サーバー起動 + curl 経由 | `logo search "..." --json` で完結 |
| コードの署名の記憶 | なし | signature を保存 |

---

## 3. コアコンセプト

### 拡張 Palace Object

searchat の palace object（4フィールド）を tree-sitter によるシンボル情報で拡張する。

```
Palace Object
├── exchange_core       "token 検証の None チェックを追加して AuthError を修正した"
├── specific_context    "validate() の戻り値が Optional[User] であることが原因だった"
├── room_assignments    [                                  ← 論文準拠の構造体配列
│                         { room_type: "concept",
│                           room_key:  "token-validation",
│                           room_label: "Token Validation",
│                           relevance: 0.9 },
│                         { room_type: "file",
│                           room_key:  "auth_middleware",
│                           room_label: "Auth Middleware",
│                           relevance: 0.8 }
│                       ]
├── files_touched       ["src/auth/middleware.py"]        ← regex 抽出（LLM非使用）
│
├── symbols_touched     ["AuthMiddleware.validate", "Token.decode"]   ← 追加
└── symbol_signatures   ["def validate(token: str) -> User | None"]   ← 追加
```

> `room_type` は `file` / `concept` / `workflow` の3種。
> `room_key` は定義済み語彙に縛られず LLM が生成する自由形式のキー。
> 重複排除は `(room_type, room_key)` のハッシュで行う（DB がプロジェクトスコープのため project_id 不要）。
> → 異なる exchange から同じ room_key が来ると同じ「部屋」にマップされる。

**Surviving Vocabulary 原則は継承する**（参加者の用語をそのまま使う）。

### 2方向の検索

```
クエリ → 会話    「connection pool の修正」→ 関連する過去会話
コード → 会話    AuthMiddleware.validate を編集中 → 関連する過去会話を surface
```

---

## 4. CLI インターフェース

### 基本検索

```bash
logo search "connection pool 枯渇"

# 出力
[1] 2026-01-15  src/db/pool.py:34  DatabasePool.__init__
    DATABASE_URL に pool_size=5 を追加して connection pool 枯渇を解決した。
    ~/.claude/projects/.../abc.jsonl  ply=42

[2] 2026-02-03  src/db/pool.py:89  PoolManager.acquire
    pool_pre_ping=True で死活確認を追加した。
```

### コードから会話を逆引き

```bash
logo context --symbol "AuthMiddleware.validate"
```

### 原文取得

```bash
logo show "~/.claude/projects/.../abc.jsonl:ply=42"        # 人間向け
logo show "~/.claude/projects/.../abc.jsonl:ply=42" --json  # LLM向け
```

### インデックス管理

```bash
logo init                         # .logosyncs/ を初期化（初回のみ）
logo index                        # 未インデックスの .jsonl を処理
logo index --path ~/.claude/projects/...
logo distill                      # 未蒸留の exchange を Haiku で処理
logo status                       # インデックス状況を表示
```

### 全件 in-context ロード（セッション開始時）

```bash
logo dump --distilled                     # 全 distilled objects を出力（デフォルト: 最新1000件）
logo dump --distilled --limit 500         # 件数指定
logo dump --distilled --json              # JSON 配列で出力（LLM 向け）

# 出力イメージ（--json 時）
[
  {
    "exchange_core": "pool_size=5 を DATABASE_URL に追加して connection pool 枯渇を解決した",
    "specific_context": "pool_pre_ping=True で死活確認も追加",
    "rooms": [
      { "room_type": "concept", "room_key": "connection-pool", "room_label": "Connection Pool" },
      { "room_type": "workflow", "room_key": "database-config", "room_label": "Database Config" }
    ],
    "date": "2026-01-15"
  },
  ...  // 1件 ≈ 38 token → 1000件 ≈ 39,000 token（200K窓に余裕で収まる）
]
```

> **設計根拠**: 論文 contribution #3「1,000 exchanges fit in ~39,000 tokens instead of ~407,000」を実現する。
> RAG（`logo search`）は「何を探すか分かっている」場合に使い、
> `logo dump` は「セッション開始時に全記憶を俯瞰したい」場合に使う。両者は補完関係。

### LLM 向け（JSON 出力）

```bash
logo search "connection pool" --json --limit 5
# → CLAUDE.md から curl なしで呼べる
```

---

## 5. データ設計

### ストレージ（`<project-root>/.logosyncs/`）

`logo init` でプロジェクトルートに初期化する（`.git` と同じモデル）。
DB はプロジェクト単位でスコープされるため、プロジェクト間の混在は起きない。

```
<project-root>/
└── .logosyncs/
    └── memory.db        verbatim + palace objects + symbol index（SQLite）
```

差分管理は `conversations.source_path` を処理済みキャッシュとして使用する。
`logo index` 実行時に `.jsonl` ファイル一覧と DB を突き合わせて未処理分のみ処理する。

SQLite 1ファイルで FTS5（BM25）・sqlite-vec（ANN）・通常テーブルを統合管理。

### テーブル構成

```sql
-- 会話単位
conversations (
  id TEXT PRIMARY KEY,       -- sha256(source_path) で生成（決定的・重複排除に使用）
  source_path TEXT,          -- 元の .jsonl へのポインタ
  started_at TIMESTAMP
)

-- exchange 単位（user + agent のターンペア）
-- 境界定義: role="user" エントリから次の role="user" 直前まで
-- ツール呼び出し・中間応答・複数アシスタントターンは同一 exchange にまとめる
exchanges (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,
  ply_start INT, ply_end INT,
  user_content TEXT,
  agent_content TEXT,
  distilled_at TIMESTAMP     -- NULL = 未蒸留・logo distill の処理対象判定に使用
  -- ※ embedding はチャンク単位で exchanges_chunks に持つ（exchange 全体を1ベクトルにしない）
)

-- verbatim 全文検索テーブル（BM25 verbatim 路線）
-- ※ exchanges_fts は exchanges テーブルの content テーブルとして作成する:
--   CREATE VIRTUAL TABLE exchanges_fts USING fts5(
--     user_content, agent_content,
--     content=exchanges, content_rowid=rowid
--   )
-- ※ HNSW(verbatim) は論文ベスト構成に含まれないため verbatim の embedding は保存しない
--   verbatim の役割は BM25(exchanges_fts) が担う

-- 蒸留済み palace object
palace_objects (
  id TEXT PRIMARY KEY,
  exchange_id TEXT,
  exchange_core TEXT,
  specific_context TEXT,
  distill_text TEXT,         -- exchange_core + "\n" + specific_context（embedding 用）
  bm25_text TEXT,            -- FTS5 の検索対象テキスト（下記フィールドを連結）
                             -- = exchange_core + specific_context + files_touched
                             --   + room_key × N + room_label × N
                             -- 論文準拠: exchange_core, specific_context, files_touched,
                             --           room_key, room_label を連結
  embedding FLOAT[384]       -- distill_text 用
)
-- ※ FTS5 仮想テーブルは bm25_text を対象に別途作成する:
--   CREATE VIRTUAL TABLE palace_fts USING fts5(bm25_text, content=palace_objects)

-- verbatim 検索用 FTS5（BM25 verbatim 路線 / 論文 Cross BM25(V)+HNSW(D) の V に対応）
-- ※ exchanges_fts は palace_fts とは独立した FTS5 テーブル
-- CREATE VIRTUAL TABLE exchanges_fts USING fts5(
--   user_content, agent_content,
--   content=exchanges, content_rowid=rowid
-- )

-- シンボルインデックス（tree-sitter 解決済み）
symbols (
  id TEXT PRIMARY KEY,
  palace_object_id TEXT,
  symbol_name TEXT,          -- "AuthMiddleware.validate"
  symbol_kind TEXT,          -- "method" / "function" / "class"
  file_path TEXT,
  signature TEXT,            -- "def validate(token: str) -> User | None"
  dedup_hash TEXT            -- (symbol_name, file_path) のハッシュ
)

-- 部屋割り当て（論文の room_assignments に準拠）
rooms (
  id TEXT PRIMARY KEY,
  palace_object_id TEXT,
  room_type TEXT,            -- "file" / "concept" / "workflow"
  room_key TEXT,             -- 自由形式キー（LLM生成）例: "retry_timeout", "auth_middleware"
  room_label TEXT,           -- 短いラベル（LLM生成）例: "Retry Timeout", "Auth Middleware"
  relevance REAL,            -- 0.0–1.0
  dedup_hash TEXT            -- hash(room_type, room_key)
                             -- 同一キーは異なる exchange からでも同じ部屋にマップされる
                             -- DB 自体がプロジェクトスコープのため project_id は不要
)
```

---

## 6. アーキテクチャ

### トリガー

```
Claude Code セッション終了
  │
  └─ Stop フック
       ├─ logo index                 （同期・即時 / embedding のみ）
       └─ nohup logo distill &       （detach して起動・Claude Code 終了後も継続）
            │ claude --print は ~/.claude/ の認証情報を使うため
            │ 親プロセス（Claude Code）の終了に依存しない
            └─ 次セッション開始までに完了していればよい
```

### INDEXER

```
~/.claude/projects/<project-hash>/*.jsonl
  │  ※ project-hash は git root のパスから自動解決（logo init 時に記録）
  │
  │ ① .jsonl を exchange 単位に分割
  │   exchange = user 発話 + agent 応答 の 1 ターンペア
  │   ※ 会話全体ではなく exchange 単位で扱う
  │
  │   フィルタ・分割ルール（論文 Section 3.1 準拠）:
  │     - 100文字未満の exchange は trivial として除外
  │     - 20 ply を超える exchange は固定間隔で分割
  │     - tool-use のみのラウンドトリップ（user の実質的な発話なし）は
  │       exchange の区切りとしない → 次の実質応答まで同一 exchange に含める
  │
  ├──────────────────────────────────────────────────┐
  │                                                  │
  ▼                                                  ▼
verbatim 路線                                  蒸留キューに積む
  │                                            （DISTILLER へ）
  │ ② exchanges_fts に登録（embedding なし）
  │   論文準拠: verbatim の役割は BM25 のみ
  │   HNSW(verbatim) は論文ベスト構成に含まれないため embedding は計算しない
  │
  ▼
exchanges テーブル（SQLite）
  user_content, agent_content
exchanges_fts 仮想テーブル（FTS5 / BM25）← exchanges の content テーブルとして構築
```

### DISTILLER（バックグラウンド）

```
蒸留キュー
  │
  │ ③ files_touched を regex で抽出（LLM 非使用・論文 Appendix B 準拠）
  │   raw exchange テキスト（user_content + agent_content）に対して正規表現を適用
  │   LLM に任せると hallucination でパスを捏造するため regex で確実に抽出する
  │
  │   抽出対象パターン例:
  │     - 相対パス: src/auth/middleware.py, lib/db/pool.ts
  │     - 絶対パス: /Users/.../project/foo.py
  │     - tool_use の file_path フィールド（jsonl 中の構造化データ）
  │     → 重複除去・存在確認なしで保存（蒸留時点のスナップショット）
  │
  │ ④ claude -p で palace object 生成（LLM が担うのは3フィールドのみ）
  │   Surviving Vocabulary 原則（参加者の用語をそのまま使う）
  │   --output-format json --json-schema で出力スキーマを公式機能で強制
  │
  │   claude -p "..." \
  │     --model claude-haiku-4-5-20251001 \    ← Haiku 必須（Sonnet以上は冗長になる）
  │     --output-format json \
  │     --json-schema '{"type":"object","properties":{
  │       "exchange_core":{"type":"string","maxLength":300},
  │       "specific_context":{"type":"string","maxLength":200},
  │       "room_assignments":{"type":"array","maxItems":3,"items":{
  │         "type":"object","properties":{
  │           "room_type": {"type":"string","enum":["file","concept","workflow"]},
  │           "room_key":  {"type":"string"},
  │           "room_label":{"type":"string"},
  │           "relevance": {"type":"number","minimum":0,"maximum":1}
  │         },"required":["room_type","room_key","room_label","relevance"]}}
  │     },"required":["exchange_core","specific_context","room_assignments"]}'
  │                      ↑ files_touched はスキーマに含めない（regex 抽出済みのため）
  │
  │   プロンプト本文（論文 Appendix B そのまま・日本語訳版・38トークン達成の要）:
  │   ※ {ply_start}, {ply_end}, {messages_text} をやり取りごとに補間
  │   ※ messages_text は 4,000文字で切り捨て
  │
  │     この対話のやり取りをJSONに蒸留してください：
  │
  │     - "exchange_core": 1-2文。何が達成または決定されましたか？
  │       やり取り内の特定の用語を使用してください。
  │       テキストに存在しない詳細を捏造しないでください。
  │       やり取りがほぼ空の場合は、簡潔にその旨を述べてください。
  │     - "specific_context": テキストからの具体的な詳細1つ：
  │       数値、エラーメッセージ、パラメータ名、またはファイルパス。
  │       テキストから正確にコピーしてください。プロジェクトパスは使用しないでください。
  │     - "room_assignments": 1-3個の部屋。各部屋はこのやり取りが属するトピックです。
  │       {"room_type": "<file|concept|workflow>", "room_key": "<識別子>",
  │        "room_label": "<短いラベル>", "relevance": <0.0-1.0>}
  │       部屋は関連するやり取りをグループ化するのに十分具体的なものにしてください
  │       （例：「errors」ではなく「retry_timeout」）。
  │
  │     "files_touched"は含めないでください。
  │
  │     やり取り (メッセージ {ply_start}-{ply_end}): {messages_text}
  │
  │     JSONのみで回答してください。
  │
  │   ※ 長さ制約の多重防御:
  │     1. プロンプトの文言で "1-2 sentences" / "One concrete detail" と明示
  │     2. --json-schema の maxLength / maxItems でフィールド上限を設定
  │     3. Haiku を使う（Sonnet以上は詳しく書きすぎて38トークンを超える）
  │
  │   → exchange_core, specific_context, room_assignments
  │
  │   ※ 蒸留は logo distill の1モードのみ。常に room_assignments を生成する。
  │     Stop フックで nohup logo distill & としてバックグラウンド実行するため
  │     タイムアウト制約はなく、タグ（簡易中間状態）という概念は不要。
  │
  │ ⑤ bm25_text を組み立てて FTS5 に登録
  │   論文準拠: exchange_core + specific_context + files_touched + room_key + room_label を連結
  │
  │   bm25_text = " ".join([
  │     exchange_core,                      # LLM生成
  │     specific_context,                   # LLM生成
  │     " ".join(files_touched),            # regex抽出（③）
  │     " ".join(r["room_key"]   for r in room_assignments),  # LLM生成
  │     " ".join(r["room_label"] for r in room_assignments),  # LLM生成
  │   ])
  │
  │   ※ 論文との差分:
  │     - 論文は毎クエリ時にメモリ上で BM25 を遅延構築（永続化しない）
  │     - SPEC は SQLite FTS5 に永続化（INSERT 時に自動更新・クエリ時の再構築コストなし）
  │     - 検索対象フィールドは同じ（exchange_core + specific_context + files + room_key + room_label）
  │
  │ ⑥ embedding 計算
  │   multilingual-e5-small（distill_text 用）
  │
  │ ⑦ Tree-sitter でシンボル解決
  │   ③ で抽出した files_touched のファイルを構文解析
  │   → symbol_name, symbol_kind, signature, line
  │
  ▼
palace_objects テーブル + palace_fts（FTS5）+ symbols テーブル（同じ memory.db）
```

### SEARCH

```
自然言語クエリ: "token 検証のバグをどう直した？"
  │
  ├─────────────────────────┐
  │                         │
  ▼                         ▼
BM25 検索                HNSW 検索
verbatim                 distilled
（FTS5・キーワード一致）  （sqlite-vec・意味的近傍）
  │                         │
  └────────────┬────────────┘
               │
         CombMNZ で融合
         （複数リストにヒットした文書を優先）
         ※ 論文ベスト構成: Cross BM25(V)+HNSW(D) / CombMNZ → MRR 0.759
         ※ HNSW(verbatim) は含めない
           理由: verbatim は長文で embedding 品質が低く、論文評価でも有意な改善なし
           verbatim の強みはキーワード一致（BM25）が担う
                    │
                    ▼
             exchange_id が確定
                    │
          ┌─────────┴──────────┐
          │                    │
          ▼                    ▼
  verbatim 原文を返す    symbols テーブルを参照
  （会話ログ全文）       ファイルパス・行番号・シンボル名
          │                    │
          └─────────┬──────────┘
                    ▼
         エージェントが受け取る結果
         {
           exchange_core,      // 何をしたか（要約）
           specific_context,   // 技術的詳細
           verbatim_ref,       // 原文の参照先
           symbols: [
             { name, file, line, signature }
           ]
         }
```

### シンボル解決の方針

**蒸留時に一度だけ実行する**（検索時ではなく）。

```
蒸留時にシンボルを解決して保存
  会話ログ → Haiku で蒸留 → Tree-sitter でシンボル解決 → symbols テーブルに保存
  メリット: 検索が速い・ファイル移動後も記録が残る
```

**Tree-sitter で取得できる情報:**
- シンボル名（関数名・クラス名・メソッド名）
- シンボルの種別（function / class / method）
- シグネチャ（ソースコードのテキストをそのまま保存。型解決はしない）
- ファイルパス・行番号

**スコープ外:** LSP による型解決・参照追跡は対象外。

対応言語（優先）: Python / TypeScript / Go

---

## 7. 実装フェーズ

### Phase 1: searchat 相当（最小動作）

- [ ] `.jsonl` を読んで exchange に分割
- [ ] multilingual-e5-small で embedding → sqlite-vec HNSW
- [ ] `logo search` で HNSW 検索
- [ ] `logo index` で手動インデックス

### Phase 2: 蒸留レイヤー

- [ ] `claude -p` で palace object 生成（Surviving Vocabulary 原則適用）
- [ ] `--output-format json --json-schema` でスキーマ強制（公式 headless モード機能）
- [ ] `logo distill` でバッチ蒸留
- [ ] Cross-layer 検索（BM25 verbatim + HNSW distilled）

### Phase 3: シンボル統合（差別化の核心）

- [ ] 蒸留時に Tree-sitter でシンボルを解決
- [ ] symbols テーブルへの保存
- [ ] `logo context --symbol` でコード→会話の逆引き

### Phase 4: 自動化 + 全件ロード

- [ ] Claude Code Stop フックで `logo index` を自動実行
- [ ] `logo index` 完了後にバックグラウンドで `logo distill` を起動
- [ ] CLAUDE.md テンプレート（LLM が自動で呼び出す設定）
- [ ] `logo status` でインデックス状況の可視化
- [ ] `logo dump --distilled [--limit N] [--json]` — 全件 in-context ロード用コマンド
      ※ palace_objects テーブルから distill_text を新しい順に N 件取得して出力するだけ
      ※ 1件 ≈ 38 token → 1000件 ≈ 39,000 token（論文 contribution #3 の実現）

---

## 8. 技術スタック候補

| レイヤー | 採用 | 理由 |
|---------|------|------|
| CLI | Python (typer) + pipx 配布 | Embedding がネイティブに動く・対象ユーザーは Python 必須環境 |
| SQLite ドライバ | sqlite-vec Python バインディング | FTS5・sqlite-vec を同一 DB で統合管理 |
| ベクトル検索 | sqlite-vec（ANN） | SQLite 拡張・Python バインディング成熟 |
| キーワード検索 | SQLite FTS5（BM25） | sqlite-vec と同じ DB ファイルで完結 |
| 蒸留 LLM | `claude -p`（公式 headless モード） | API キー不要・`--json-schema` でスキーマ強制が公式サポート |
| Embedding | sentence-transformers（multilingual-e5-small） | 日本語+英語混在対応・384次元・CPU動作・118Mパラメータ。cold start が問題になった場合は `backend="onnx"` で ONNX Runtime に切り替え（量子化なし・モデル互換性維持） |
| シンボル解決（Phase 3） | tree-sitter（Python バインディング） | 軽量・言語サーバー不要 |
| 自動化 | Claude Code Stop フック | デーモン不要・セッション終了時に自動実行 |

---

## 9. 未解決の設計判断

- [x] **CLI の言語**: Python (typer) + pipx 配布で確定。Embedding がネイティブに動くため Go より適切。
- [x] **シンボル解決の深さ**: Tree-sitter で確定。LSP はスコープ外。
- [x] **ツール名**: logosyncs、コマンド名 `logo` で確定
- [x] **CLAUDE.md テンプレート**: 下記セクション参照
- [x] **マルチエージェント対応**: Phase 1 は Claude Code（`~/.claude/projects/**/*.jsonl`）固定。
      将来的に他の CLI 系エージェントへ拡張する方針。
      拡張時の設計方針:
        - INDEXER にエージェント別パーサーを追加（フォーマット差異を吸収）
        - `conversations.agent_type` カラムで発生元エージェントを記録
        - `logo init --agent <claude|codex|...>` で対象を指定
        - 検索・蒸留レイヤーはエージェント非依存のまま維持
      参考: searchat は Claude Code / Codex / Mistral Vibe の `.jsonl` をパーサー切り替えで対応
- [ ] **git 統合**: ブランチ・コミットの記録方法（取得タイミングの問題あり・後回し）

---

## 10. CLAUDE.md テンプレート

プロジェクトの `CLAUDE.md` に以下を追加することでエージェントが自律的に logosyncs を使えるようになる。

````markdown
## Past Memory Search (logosyncs)

過去の実装・意思決定・コードの位置を検索するには `logo search` を使う。

### いつ使うか

- 「〜はどこに実装した？」「〜ってどこだっけ？」と聞かれたとき
- 過去に同じバグを直したか確認したいとき
- ある機能がすでに実装済みか調べたいとき
- 実装の意思決定の理由を確認したいとき

### 検索

\`\`\`bash
logo search "クエリ" --json --limit 5
\`\`\`

出力:
\`\`\`json
[
  {
    "exchange_core": "何をしたかの要約",
    "specific_context": "技術的詳細",
    "rooms": [
      { "room_type": "concept", "room_key": "auth-fix", "room_label": "Auth Fix" }
    ],
    "symbols": [
      { "name": "AuthMiddleware.validate", "file": "src/auth/middleware.py", "line": 45, "signature": "def validate(token: str) -> User | None" }
    ],
    "verbatim_ref": "~/.claude/projects/.../abc.jsonl:ply=42"
  }
]
\`\`\`

### 原文の取得

検索結果の `verbatim_ref` を使って会話の原文を取得できる:

\`\`\`bash
logo show "~/.claude/projects/.../abc.jsonl:ply=42" --json
\`\`\`

### コードから逆引き

編集中のファイル・シンボルに関連する過去会話を取得:

\`\`\`bash
logo context --symbol "AuthMiddleware.validate" --json
\`\`\`
````

---

## 参考

- searchat: [github.com/Process-Point-Technologies-Corporation/searchat](https://github.com/Process-Point-Technologies-Corporation/searchat)
- 論文: [arXiv:2603.13017](https://arxiv.org/abs/2603.13017) Structured Distillation for Personalized Agent Memory
---

## メモ（実装時に検討）

- **Embedding モデルのロード遅延**: multilingual-e5-small（118M）をセッションごとにロードする cold start コスト。同種ツール（sui-memory 等）もローカルモデルを同様に起動しており、許容されているパターン。実装時に実測して問題なら `SentenceTransformer(..., backend="onnx")` で ONNX Runtime に切り替える（量子化なし・モデル互換性維持・JIT コンパイルスキップで高速化）。
- **初回モデルダウンロード**: `logo init` 時に約500MBのモデルを自動ダウンロードする必要がある。ユーザーへの告知・プログレス表示の設計を実装時に検討する。
- **チーム共有は現状スコープ外**: `source_path` がローカルパス依存・SQLite がバイナリで git マージ不能なため、現状はパーソナルツールとして割り切る。将来的にやるなら palace objects を JSONL エクスポートして `logo import` で取り込む設計が候補。
