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
Claude Code ラリー終了（毎ターン）
  └─ Stop フック（async: true）
       └─ logo index          （ノンブロッキング / embedding のみ）

セッション境界（/clear・/resume・CC終了）
  └─ SessionEnd フック
       └─ nohup logo distill &（detach・CC終了後も継続）
            │ claude --print は ~/.claude/ の認証情報を使うため
            │ 親プロセス（Claude Code）の終了に依存しない
            └─ 次セッション開始までに完了していればよい
```

> **SessionEnd の reason 対応**: `clear`（/clear）・`resume`（/resume）・`logout`・`other`（CC終了）
> Stop フックの `async: true` は CC ネイティブの非同期実行。nohup 不要でラリーをブロックしない。

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
  │     - 50文字未満の exchange は trivial として除外
  │       ※ 論文は「4トークン未満」を閾値とするが、実データ分析に基づき 50文字を採用:
  │         - このプロジェクトの 386件の exchange を実測した結果:
  │             >= 10文字: 382件 (99.0%)
  │             >= 20文字: 375件 (97.2%)
  │             >= 50文字: 363件 (94.0%)
  │             >= 100文字: 342件 (88.6%)
  │         - 50〜99文字帯に有用なやりとりが複数確認された
  │           例: 「論文にはなんと書かれている？」「E2Eテストの設計をしたい。」
  │               「コアコンセプトにLSP情報で拡張すると書いてあるけど」
  │         - 100文字は論文の閾値（≈4〜8文字）に対して過剰に厳しかった
  │         - tool-use のみで agent がテキストを返さない exchange は
  │           combined が短くなるが、_is_real_user_entry により
  │           user_text 空の中間ステップは既に除外されているため問題ない
  │     - 20 ply を超える exchange は固定間隔で分割
  │     - tool-use のみのラウンドトリップ（user の実質的な発話なし）は
  │       exchange の区切りとしない → 次の実質応答まで同一 exchange に含める
  │
  │   _is_real_user_entry による事前除外（trivial フィルタより前に適用）:
  │     - user_text が空の entry → 中間ステップとして除外
  │       ※ agent が自律的に次のアクションを起こすターンはユーザー発話なし
  │     - tool_result のみの entry → CC がツール結果を user ロールで返す仕様
  │       実質的なユーザー発話ではないため除外
  │     - コンパクション要約（"This session is being continued..." 等）→ 除外
  │       CC のセッション引き継ぎテキストは exchange 境界としない
  │     ※ 実測: 386件の有効 exchange に対し、これらの除外により
  │        中間ステップが trivial フィルタに混入しないことを確認済み
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
         RRF (Reciprocal Rank Fusion) で融合
           score(d) = Σ 1 / (k + rank_i(d))   k=60（標準値）
           スコアの絶対値に依存せず順位だけで融合するため正規化不要

         ※ 採用根拠（arXiv:2603.13017 の実験結果に基づく）:
           論文は 107 構成の BM25/ベクトル検索を比較評価した（201件のリコールクエリ）。
           主要発見:
             - BM25 単独 20 構成: 全て有意に劣化 (効果量 |d|=0.031–0.756)
             - ベクトル検索 20 構成: Bonferroni 補正後も有意な劣化なし
             - 最良構成: Cross BM25(V)+HNSW(D) → MRR 0.759
           → BM25 単独は全クエリタイプで劣化する。コーディングエージェントの
             クエリが識別子・パス中心であっても、BM25 のみに絞る根拠はない。
           → 論文の全クエリタイプ（conceptual / phrase / exact term）で
             Cross BM25(V)+HNSW(D) が最良。QueryClassifier によるタイプ別
             切り替えを行わず、全クエリに同じ構成を適用するのが最もロバスト。

         ※ CombMNZ ではなく RRF を採用する理由:
           CombMNZ は hit_count 乗数により、両リストに出た HNSW 偽陽性が
           BM25 完全一致を押しのける構造的な問題がある。
           RRF は順位ベースのため hit_count 問題が発生せず、
           スコア正規化も不要でシンプル。論文は CombMNZ を評価しているが、
           RRF は同等以上の性能が多くの先行研究で示されている（Cormack et al., 2009）。

         ※ QueryClassifier を採用しない理由:
           「keyword クエリは BM25 のみで十分」という仮説は実証されていない。
           論文は BM25 単独が有意に劣化することを示しており、
           keyword クエリも例外ではないと考えるのが自然。
           分類ミスのリスクを抱えるより、全クエリに同じ構成を使う方がロバスト。

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
- [ ] RRF による融合（CombMNZ・QueryClassifier を採用しない理由は SEARCH セクション参照）

### Phase 3: シンボル統合（差別化の核心）

- [ ] 蒸留時に Tree-sitter でシンボルを解決
- [ ] symbols テーブルへの保存
- [ ] `logo context --symbol` でコード→会話の逆引き

### Phase 4: 自動化 + 全件ロード

- [ ] Claude Code **Stop フック**（`async: true`）で `logo index` を自動実行（毎ターン・ノンブロッキング）
- [ ] Claude Code **SessionEnd フック**（`nohup ... &`）で `logo distill` をセッション境界時のみ起動
      （`/clear`・`/resume`・CC終了をトリガーとし、毎ターンの蒸留コストを回避）
- [ ] `logo hook install` で両フックを `~/.claude/settings.json` に一括登録
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
- [x] **融合アルゴリズム**: CombMNZ → RRF (Reciprocal Rank Fusion) に変更。
      arXiv:2603.13017 の実験結果（107構成比較・201クエリ）に基づく。
      BM25 単独は全構成で有意に劣化、Cross BM25(V)+HNSW(D) が全クエリタイプで最良（MRR 0.759）。
      融合手法は論文の CombMNZ ではなく RRF を採用:
        - CombMNZ の hit_count 乗数は keyword クエリで HNSW 偽陽性を過剰評価する構造的問題がある
        - RRF は順位ベースのためその問題がなく、スコア正規化も不要
      QueryClassifier は採用しない:
        - 「keyword クエリは BM25 のみで十分」は実証されていない仮説
        - 論文は BM25 単独が有意に劣化することを示しており keyword クエリも例外ではないと考えるのが自然
        - 全クエリに同じ構成を適用する方が分類ミスのリスクなくロバスト
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

## 11. `claude -p` 蒸留運用上の問題と解決策

> Zenn 記事素材。実装中に踏んだ落とし穴と対処をまとめる。

### 問題① Stop フックがラリーをブロックする

**症状**: Stop フックに `logo index && logo distill` を同期実行で登録したため、Claude が応答を返すたびに数秒待ちが発生し UX が壊滅した。

**原因**: `logo index` が同期実行でブロックしていた。

**解決**: Stop フックを `async: true`（CC ネイティブ非同期）に変更し、`logo index` をノンブロッキング化。`logo distill` は Stop フックから外してセッション境界のみで実行するよう分離した。

---

### 問題② SessionEnd フックの無限ループ

**症状**: `logo distill` を登録した SessionEnd フックが多重起動し、複数の `claude --print` プロセスが積み重なった。最長で数時間前から走り続けるプロセスが残っていた。

**原因**: `claude --print` 自体が CC セッションとして扱われ、そのセッション終了時に SessionEnd フックが再発火するループが発生していた。

```
SessionEnd → logo distill → claude --print → SessionEnd → logo distill → ...
```

**解決**: `claude --print` 呼び出しに `--setting-sources ""` を追加。サブプロセスが `~/.claude/settings.json` を読まなくなり、フック再発火が止まる。さらに SessionEnd → **SessionStart** に変更することで根本的にループ構造を回避（`claude --print` は SessionStart を発火しない）。

---

### 問題③ `claude --print` 1 回で 28K tokens 消費

**症状**: `logo distill --limit 1` を実行しただけで CC Pro のレートリミットに当たった。

**原因**: `claude --print` はインタラクティブセッションと同等のコンテキスト初期化を行い、プロジェクトの CLAUDE.md（約 28K tokens）をキャッシュ作成していた。`cwd='/tmp'` に変えても `~/.claude` ユーザーレベル設定の読み込みは防げなかった。同症状の公式 Issue: [anthropics/claude-code#12333](https://github.com/anthropics/claude-code/issues/12333)

**解決**: 以下 2 フラグを組み合わせることで cache_creation を 27K → 4K に削減。

```bash
claude --print \
  --no-session-persistence \   # セッション保存無効・コンテキスト初期化を抑制
  --setting-sources "" \       # user/project/local 設定をスキップ → CLAUDE.md 非読込
  ...
```

実測値:

| フラグ | cache_creation | cache_read |
|--------|---------------|-----------|
| なし（初期実装） | 27,272 | 13,635 |
| `--no-session-persistence` のみ | 12,426 | 26,513 |
| `+ --setting-sources ""` | **4,232** | 20,808 |

`cache_read` は `cache_creation` の 10 倍安価なため、実質コストは大幅削減。

なお `--bare` フラグ（公式推奨のスクリプトモード）を使えばさらに削減できるが、OAuth 認証非対応のため API キーが必要。CC Pro ユーザーには使えない。

> ドキュメントより: `--bare` is the recommended mode for scripted and SDK calls, and **will become the default for `-p` in a future release.**

---

### 問題④ `--json-schema` フラグがタイムアウトする

**症状**: `--json-schema` を渡すと 30 秒以上待ってもレスポンスが返らないか、`result` フィールドに会話的な応答が入り JSON パースに失敗した。

**原因**:
1. `--output-format json` のレスポンスは `{"type":"result","result":"..."}` のラッパー構造。`result` フィールドを直接パースしていたのが誤り。
2. `--json-schema` 使用時の構造化出力は `result` ではなく **`structured_output`** フィールドに入る（ドキュメント参照）。
3. 初回呼び出しはグラマーコンパイルが発生し数十秒かかる（Anthropic ドキュメント記載）。

**解決**: `structured_output` フィールドを優先参照するように修正。searchat の実装を参考に確認。

```python
outer = json.loads(result.stdout)
if "structured_output" in outer and outer["structured_output"]:
    return outer["structured_output"]   # ← 正しいフィールド
```

---

### 問題⑤ 多重プロセス起動（プロセスロックなし）

**症状**: 複数の CC インスタンス（Zed・ターミナル・他プロジェクト）が同時に SessionEnd/SessionStart を発火し、`logo distill` プロセスが並列に複数起動した。

**解決**: ファイルベースのプロセスロックを実装。起動時に `.logosyncs/distill.lock` に PID を書き込み、起動済み PID が生きていれば即 exit。強制終了後の残骸ロックも PID 死活確認で自動クリア。

```python
if lock_path.exists():
    existing_pid = int(lock_path.read_text().strip())
    os.kill(existing_pid, 0)  # PID 生死確認（ProcessLookupError で死亡判定）
    raise typer.Exit(0)       # 生きていれば即終了
```

2 つ目以降はキューに積まれず**即破棄**される。次の SessionStart 発火時に未蒸留分をまとめて処理するため取りこぼしは発生しない。

---

### 問題⑥ sqlite-vec virtual table が `INSERT OR IGNORE` 非対応

**症状**: `logo distill` 実行時に `OperationalError: UNIQUE constraint failed on vec_palace primary key` が発生。

**原因**: sqlite-vec の virtual table は通常の SQLite テーブルと異なり `INSERT OR IGNORE` の conflict resolution をサポートしない。

**解決**: 存在チェック後に INSERT する方式に変更。

```python
exists = con.execute("SELECT 1 FROM vec_palace WHERE palace_id = ?", (palace_id,)).fetchone()
if not exists:
    con.execute("INSERT INTO vec_palace (palace_id, embedding) VALUES (?, ?)", ...)
```

---

### 問題⑦ フックのトリガー選択（SessionEnd vs SessionStart）

**結論**: `SessionEnd` より `SessionStart` が優れている。

| | SessionEnd | SessionStart |
|--|-----------|-------------|
| ループリスク | `claude --print` が再発火する可能性あり | `claude --print` は発火しない |
| タイムアウト | デフォルト 1.5 秒制限あり | 制限なし |
| タイミング | セッション終了時（次の indexing 未完の可能性） | 新セッション開始時（前回分の indexing 完了後） |

SessionStart の matcher: `startup\|clear\|resume\|compact`

---

## 12. `logo search` パフォーマンスチューニング

### 問題: 毎回 7 秒かかるコールドスタート

**症状**: `logo search` を実行するたびに 7〜8 秒かかる。エージェントが自律的に使うには許容できない遅延。

**原因**: `logo search` は毎回新しい Python プロセスを起動し、`multilingual-e5-small`（500MB）のモデル重みをゼロからロードしていた。

```
$ time logo search "RRF 採用理由"
real  0m7.6s   ← ほぼ全部モデルロード時間
```

---

### 解決: Unix ソケットサーバー方式

モデルを常駐させる軽量サーバーを実装し、`logo search` はソケット経由でクエリを投げるだけにした。

**アーキテクチャ:**

```
logo search "..." 
  │
  ├─ .logosyncs/embedder.sock が存在する？
  │     YES → ソケット経由でクエリ送信 → 0.15秒
  │     NO  → 直接モデルロード（~7秒）
  │            └─ バックグラウンドでサーバーを起動（次回から高速）
  │
  └─ 結果を返す

embedder_server（常駐プロセス）
  - Unix ソケット .logosyncs/embedder.sock でリッスン
  - モデルをメモリに保持
  - リクエストごとにスレッドで処理
  - 10分間アイドルで自動終了（CPU 0%、メモリ ~500MB）
```

**実測値:**

| 状態 | 検索時間 |
|------|---------|
| サーバーなし（初回） | ~7.5 秒 |
| サーバー起動済み（2回目以降） | **0.15 秒**（50倍速） |

**ライフサイクル:**

- `SessionStart` フックで `nohup logo server start > /dev/null 2>&1 &` を実行 → CC 起動時に自動ウォームアップ
- 10分アイドルでサーバー自動終了（明示的な停止不要）
- 次の SessionStart で再起動

**デッドロックの落とし穴:**

サーバー内で `Embedder()` を使う際、`_find_sock_path()` が自分自身のソケットを発見して自己接続し、デッドロックが発生した。

```
Server → embedder.embed() → _try_socket_embed(自分) → Server 待機 → デッドロック
```

**解決**: サーバー起動時に `LOGO_NO_SOCK=1` 環境変数をセットし、`Embedder` がソケットを探さないようにした。

```python
os.environ["LOGO_NO_SOCK"] = "1"
embedder = Embedder()   # ソケット不使用・直接モデルロード
del os.environ["LOGO_NO_SOCK"]
```

---

## 13. インデックス除外ルール

### コンパクション要約の除外

**問題**: CC がコンテキスト圧縮（`/compact`）を行うと、再開セッションの先頭に長大な要約テキストが `role: "user"` として挿入される。これをそのまま exchange に含めると：

- 蒸留結果に要約が混入し、実際の会話内容と区別できなくなる
- `logo context --symbol` の結果が要約テキストで汚染される
- トークン無駄遣い（要約は数千文字に及ぶ）

**解決**: `_is_real_user_entry()` でコンパクション要約のプレフィックスを検出し、exchange 境界として扱わない。

```python
_COMPACT_PREFIXES = (
    "This session is being continued from a previous conversation",
    "前のセッションからの引き継ぎです",
    "このセッションは、以前の会話から引き継がれています",
)
```

実際の `.jsonl` で確認したパターン:

```
"This session is being continued from a previous conversation that ran out of context.
 The summary below covers the earlier portion of the conversation.\n\nSummary:\n..."
```

**注意**: 既にインデックス済みの exchange には要約が混入している場合がある。再インデックスしない限り残る。

---

## 14. tree-sitter シンボル解決のユースケースと改善案

### 現状

蒸留時に `files_touched` からシンボルを tree-sitter で抽出し、`symbols` テーブルに保存する。`logo context --symbol <name>` で「このシンボルに関連する過去会話」を逆引きできる。

### ユースケース

**① コードから会話を逆引き（実装済み）**

```bash
logo context --symbol "distill"
# → distill 関数に関連する過去の設計議論・実装経緯が返る
```

「なぜこの実装にしたか」「どんな制約を決めたか」を即座に確認できる。

**② エージェントが編集前に文脈を自動取得（最重要ユースケース）**

CLAUDE.md に以下を記述しておくことで、CC がファイルを編集しようとしたとき `logo context --symbol` を自動実行できる：

```bash
logo context --symbol "変更対象の関数名" --json
```

エージェントが「自分がこれから触るコードについて過去に何を決めたか」を自律的に参照できる。毎回人間が説明する必要がなくなる。このツールの根幹的な価値と直結する。

**③ リファクタリング前のリスク把握**

関数名・シグネチャを変える前に過去議論を確認し、設計上の制約の見落としを防ぐ。

**④ バグ調査**

「このクラスを触った会話を全部出して」で、バグが入り込んだ経緯を会話ログから追える。

### 改善案

- **PreToolUse フックとの統合**: CC が Edit/Write ツールを使う前に対象ファイルのシンボルを自動検索し、関連する過去会話を context として注入する
- **シンボル変更の追跡**: 同一シンボルのリネームを検出して過去の参照を維持する（現在はシンボル名の完全一致のみ）
- **`logo context --file <path>` の充実**: ファイル単位での逆引きをシンボル一覧とともに返す

---

## 参考

- searchat: [github.com/Process-Point-Technologies-Corporation/searchat](https://github.com/Process-Point-Technologies-Corporation/searchat)
- 論文: [arXiv:2603.13017](https://arxiv.org/abs/2603.13017) Structured Distillation for Personalized Agent Memory
---

## メモ（実装時に検討）

- **Embedding モデルのロード遅延**: multilingual-e5-small（118M）をセッションごとにロードする cold start コスト。同種ツール（sui-memory 等）もローカルモデルを同様に起動しており、許容されているパターン。実装時に実測して問題なら `SentenceTransformer(..., backend="onnx")` で ONNX Runtime に切り替える（量子化なし・モデル互換性維持・JIT コンパイルスキップで高速化）。
- **初回モデルダウンロード**: `logo init` 時に約500MBのモデルを自動ダウンロードする必要がある。ユーザーへの告知・プログレス表示の設計を実装時に検討する。
- **チーム共有は現状スコープ外**: `source_path` がローカルパス依存・SQLite がバイナリで git マージ不能なため、現状はパーソナルツールとして割り切る。将来的にやるなら palace objects を JSONL エクスポートして `logo import` で取り込む設計が候補。
