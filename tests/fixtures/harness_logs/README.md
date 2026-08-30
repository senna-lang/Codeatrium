# 合成ハーネスログ（design §11.1）

5ハーネス（claude / codex / omp-pi / opencode / grok）の編集記録の**形**を手で写した合成ログ。
実ログから内容を無害なダミーに置き換えたもの。**会話本文・絶対パス・他プロジェクト名は含まれていない。**

実ログはこのリポジトリに絶対に置かない（ローカルの `~/codeatrium-fixtures/` にのみ退避、コミット禁止）。
ここにあるファイルだけがテストに使ってよいデータ。

各ファイルの形は、2026-08-08 に実ログ（Claude 7本 / codex 154本 / omp-pi 99本 / grok 39本 / opencode.db 1本）
を実際に読んで検証したキー名・ネスト構造をそのまま反映している。design doc §2.1 の要約と食い違う点は
実ログを優先し、本 README に注記した。

## claude.jsonl

`.jsonl`。1行1エントリ。`toolUseResult`（structuredPatch / oldString / newString）は
**assistant の tool_use ブロックではなく、対応する user エントリ**に載る。`tool_use_id` で対応付ける。

含む行:
1. user: 依頼文
2. assistant: `tool_use`（Edit）
3. user: `tool_result` + `toolUseResult`（`structuredPatch` あり、`oldString`/`newString` あり）— 正常系
4. assistant: `tool_use`（Write）
5. user: `tool_result` + `toolUseResult`（`type: "create"`, `structuredPatch: []`, `content` に全文）— 新規ファイル
6. assistant: `tool_use`（Edit）
7. user: `tool_result` + `toolUseResult` に `filePath` しか無い — 異常系（差分の項目が無い、FileOnly のみに落ちる）

## codex.jsonl

`.jsonl`。envelope は `{timestamp, type, payload}`。編集記録は `type: "event_msg"` の
`payload.type == "patch_apply_end"` にある（**tool_use ブロックではない** — 当初 `exec` のみを見て
「記録が無い」と誤判定した経緯が design §2.1 にある。payload 種別を全部数えること）。

`payload.changes` は `{絶対パス: {type, unified_diff | content, move_path}}`。
`move_path` キーは**常に存在し**、リネームでなければ `null`。

含む行: `session_meta` → `turn_context` → user/assistant `response_item` →
`patch_apply_end`（`update`/`add`/`delete` を含む複合パッチ）→
`patch_apply_end`（`move_path` にリネーム先が入る異常系）→
`patch_apply_end`（`changes: {}` — 差分の項目が無い異常系）。

## omp_pi.jsonl

`.jsonl`。envelope は `{type: "message", id, parentId, timestamp, message: {role, content}}`。
role は `user` / `assistant` に加えて **`toolResult` が独立したロールとして存在する**
（Claude のように user メッセージに埋め込まれない）。

編集は `content` 内の `{type: "toolCall", name: "edit", arguments: {i, input}}`。
`input` は独自形式のパッチ本文で `[path#hash]` ヘッダの後に `SWAP n.=m:` / `INS.HEAD:` / `MV a -> b`
などのコマンドが続く（実測 SWAP 655 / INS 212 / PUT 134 / DEL 51 / CUT 2 / MV 2、design §3.3 参照）。
**v1 では解読せず、`input` 全体を `TextAnchor.new_string` としてそのまま使う**
（omp は `anchor` capability 扱い）。
`write` は `arguments` に `input` ではなく `path` / `content` を持つ点に注意。

**2026-08-30 にアダプター実装のため実ログ99本を再調査し、当初の記載に無かった形を追加した**
（この5点はいずれも取りこぼし・誤ひも付けに直結する）:

1. **パスの正は toolCall ではなく toolResult 側**。toolCall のヘッダは入れ子の作業ディレクトリ
   基準に切り詰められることがあり（実測1014件中95件が不一致。すべて basename は一致し
   プレフィックスだけが欠ける。例: 呼び出し `config.py` → 実際 `src/codeatrium/config.py`）、
   そのまま cwd と結合すると実在しないパスへひも付ける。`toolCallId` で対応する toolResult 本文の
   `[path#hash]` ヘッダを優先すること。
2. **パスの大半は相対**（edit ヘッダ 323/343・write の `path` 475/568）。絶対化には
   `{type: "session", cwd}` エントリが要る（実測で常に2行目、`version: 3`）。
3. **`*** Begin Patch` 前置き**が実測263件。その場合ヘッダは常に次行にある（263/263）。
4. **1回の toolCall が複数ファイルを含む**（実測で最大6ヘッダ）。toolResult 側もヘッダ数が
   一致するので順番に対応付けられる。
5. **ファイルではない書き込み先**がある。`write` の `path` が `xd://mcp__serena_...` 形式の
   URI（実測326件）。また toolResult に JSON 配列がそのまま出力され、`[{...}]` の1行が
   素朴なヘッダ正規表現に誤マッチする。

`edit` の `arguments` は4形（`{i,input}` 327 / `{input}` 263 / `{input,path}` 28 /
`{edits,i,path}` 4）。`edits` は `{old_text, new_text}` の配列で TextAnchor へ直接写せる。
`partialArgs` / `streamIndex` を持つ toolCall（114件）は中断ではなく完了済みで、
`arguments` も完全（全件に toolResult が存在することを確認済み）。

含む行: title → session(cwd) → user → assistant(`edit`, ヘッダ切り詰め) → toolResult(解決済みパス) →
assistant(`write`, 相対 path + partialArgs) → toolResult →
assistant(`edit`, `*** Begin Patch` + 複数ファイル) → toolResult →
assistant(`edit`, `edits` 配列) → toolResult → **user（2ターン目）** →
assistant(`edit`, MV) → toolResult → assistant(`write`, `xd://` 異常系) → toolResult →
assistant(`edit`, ヘッダも path も無い異常系) → toolResult → assistant(text) →
developer → custom。

## grok.jsonl

`.jsonl`。ACP (Agent Client Protocol) 形式: `{timestamp, method: "session/update", params: {sessionId, update}}`。
1つの編集につき2エントリ:

1. `update.sessionUpdate == "tool_call"` — `rawInput` に `file_path` / `old_string` / `new_string`
   （`search_replace`）または `file_path` / `content`（`write`）
2. `update.sessionUpdate == "tool_call_update"` — `content: [{type: "diff", path, oldText, newText}]`

**design §2.1 の記載訂正**: 「行番号が無い」は正しいが、フィールド名は `old_string`/`new_string`
（`rawInput` 側）と `oldText`/`newText`（`diff` content 側）の**2種類が両方存在する**。
どちらも文字列マッチ用の `TextAnchor` に変換できる。

**2026-08-30 にアダプター実装のため実ログ39本を再調査して追記**:

1. **1つの `toolCallId` に更新が複数回届く**。`in_progress` と `completed` で同じ diff ブロックが
   2回来るのが 600件中595件。エントリを素朴に列挙すると編集記録が**二重になる**。
   `toolCallId` ごとに1件へ畳み込むこと。
2. **パスの正は `rawInput` ではなく diff ブロック**。`rawInput.file_path` は相対のことがあり
   （実測60/600）、diff 側は常に絶対パス。これを使えば cwd を引く必要がない。
3. ツールの識別は `name` ではなく **`title`**（`write` 320 / `search_replace` 280）。
4. **編集に行番号は無い**。`locations` は `read_file` など別ツールにしか付かず、
   `write`/`search_replace` には1件も無い（`anchor` capability の裏付け）。
5. `timestamp` は **整数 epoch 秒**（実測819/819）。他ハーネスの ISO 文字列に揃える必要がある。
6. method は `session/update` と **`_x.ai/session/update`** の2系統。`hook_execution` が3651件
   混ざるので `sessionUpdate` で判定する（`method` では絞らない）。
7. 会話は `user_message_chunk` / `agent_message_chunk` で届くが、実測ではどちらも
   連続分割されない（連続run長は全て1）。`agent_thought_chunk` は思考なので本文に含めない。

**実ログの置き場所**: `~/.grok/sessions/<cwd を percent-encode したディレクトリ>/<session-uuid>/updates.jsonl`
（`/` も `%2F` になる）。同じ階層に別形式の `prompt_history.jsonl` / `events.jsonl` が同居するため、
`*.jsonl` で拾うと誤って読み込む。

含む行: user_message_chunk → agent_thought_chunk → hook_execution（ノイズ） →
`search_replace` の tool_call（絶対パス）→ tool_call_update ×2（diff 重複）→
`write` の tool_call（**相対 rawInput**）→ tool_call_update（絶対 diff パス、`oldText: ""`）→
`search_replace` の失敗（`status: "failed"`）→ `read_file`（編集以外）→
agent_message_chunk → turn_completed。

## opencode.json

opencode は `.jsonl` ではなく **SQLite**（`~/.local/share/opencode/opencode.db`）。
テキストで手書き・レビューできる形にするため、`project` / `session` / `messages` / `parts` の
各テーブル行を JSON で表現した（実際の列は `message(id, session_id, data JSON)` /
`part(id, message_id, session_id, data JSON)` — ここでの `data` の中身は実ログのまま）。

編集は `part.data` が `{type: "tool", tool: "edit"|"write", state: {status, input, output, metadata}}`。
`state.input` に `oldString`/`newString`（edit）または `content`（write）が直接入っており、
`state.metadata.diff` に unified diff も別途ある（両方使える＝`LineRange` は diff から、
`TextAnchor` は `input` から作れる）。

**実ログで見つかった注意点**: write の `state.input.filePath` は相対パス（`./result.py`）のことがあるが、
`state.metadata.filepath`（小文字！）に解決済みの絶対パスが入る。どちらを見るかを adapter 実装時に
決めること。

含む部品: edit（正常系）、write（正常系、相対/絶対パスの食い違いを含む）、
write が権限拒否で失敗する異常系（`state.status == "error"`, `metadata` 無し）、
ターン全体のファイル一覧を持つ `type: "patch"` パート（個々の edit/write とは別物）。

## 使い方

各ハーネスのアダプター実装時（design §7.1 ステップ8）、このディレクトリのファイルを
`extract_code_touches` のテストデータとして読み込む。Claude アダプターの既存テスト
（`tests/test_adapters_harness_claude.py`）は今のところ辞書をインラインで組み立てており
`claude.jsonl` は読んでいない — 形の保存が目的で、消費方法を強制するものではない。
