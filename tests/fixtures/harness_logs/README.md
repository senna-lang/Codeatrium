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
などのコマンドが続く（実測で6種類確認、design §3.3 参照）。**v1 では解読せず、`input` 全体を
`TextAnchor.new_string` としてそのまま使う**（omp は `anchor` capability 扱い）。
`write` は `arguments` に `input` ではなく `path` / `content` を持つ点に注意。

含む行: user → assistant(`edit`, SWAP 1件) → toolResult → assistant(`write`) → toolResult →
assistant(`edit`, INS.HEAD+SWAP+MV の複合パッチ) → toolResult。

## grok.jsonl

`.jsonl`。ACP (Agent Client Protocol) 形式: `{timestamp, method: "session/update", params: {sessionId, update}}`。
1つの編集につき2エントリ:

1. `update.sessionUpdate == "tool_call"` — `rawInput` に `file_path` / `old_string` / `new_string`
   （`search_replace`）または `file_path` / `content`（`write`）
2. `update.sessionUpdate == "tool_call_update"` — `content: [{type: "diff", path, oldText, newText}]`

**design §2.1 の記載訂正**: 「行番号が無い」は正しいが、フィールド名は `old_string`/`new_string`
（`rawInput` 側）と `oldText`/`newText`（`diff` content 側）の**2種類が両方存在する**。
どちらも文字列マッチ用の `TextAnchor` に変換できる。

含む行: `search_replace` の tool_call→tool_call_update（正常系）、`write` の同様のペア、
`search_replace` が `old_string not found` で失敗する異常系（`tool_call_update.status == "failed"`,
`content: []`）。

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
