# 設計: 蒸留トリガー(hooks)の harness 抽象化

作成日: 2026-09-06
対象: codeatrium (`loci`) — `src/codeatrium/adapters/harness/hooks.py`
ステータス: 設計のみ（未実装）

---

## 0. 結論

現状 `ClaudeHooks`/`CodexHooks` は「lifecycle イベント → loci コマンド」の対応表を
クラスごとに**独立してリテラル定義**しており、同じ知識が重複している（§1）。
`omp-pi`/`opencode`/`grok` は `FallbackHooks` でテキストの手順を返すだけで実装がゼロ
（`install()` は常に `return False, ...`）。

しかし実機調査の結果（§2、本日このマシン上で実証済み）、**3つとも実際にはネイティブな
hook/plugin機構を持っている**ことが判明した。`docs/internal/LOCAL-FIRST-harness-matrix.md`
（2026-07-23付）の「omp-pi: TBD/fallback濃厚」「opencode: native hooks未検出」
「grok: install API不明」という判定は当時の未確認情報であり、更新が必要。

設計方針:
1. lifecycle→command の対応を1箇所の正規定義に集約し、重複を除去する
2. harness を「書き込み先の**ファイル所有モデル**」で2系統に分類し、系統ごとに
   writer 実装を共有する（フォーマットではなく所有モデルで分類するのが肝）

蒸留に使う**モデル**（`ollama-ft`/`claude-cli`/`openai-compat` の選択）は harness と
直交しており、今回の変更対象ではない（§4.4）。

---

## 1. 現状の重複（証拠）

- `ClaudeHooks.install()` → `codeatrium.hooks.install_hooks()` が
  「`Stop`→`loci index`」「`SessionStart`→`server start`/`distill`/`prime`」の
  4コマンドを構築
- `CodexHooks.install()`（`hooks.py:57-69`）が**全く同じ4コマンドを再度リテラルに
  構築**（イベント名の綴りだけ揃えて書き直している）
- 5つ目の harness を足すたびに「どのイベントで何を呼ぶか」の知識をコピペする設計に
  なっている。単一の正規情報源が無い状態（`~/.claude/CLAUDE.md` の「定義の変更を
  利用側へ追随させる」原則に反する）

---

## 2. 実地調査（2026-09-06, このマシン上）

### omp-pi

- `~/.omp/agent/extensions/*.ts` — ディレクトリ自動検出（`config.yml` に登録不要、
  実証済み: `grep -i extension ~/.omp/agent/config.yml` はヒットなし）
- API: `export default function(pi) { pi.on(eventName, (event, ctx) => {...}) }`
- 観測済みイベント（既存の `resumex-hook.ts` / `herdr-omp-agent-state.ts` /
  `adrafinil.ts` から実証）: `session_start`, `session_switch`, `agent_start`,
  `agent_end`, `session_shutdown`, `tool_approval_requested/resolved`,
  `tool_execution_start/end`
- ファイル所有モデル: **ツールごとに専用ファイル1本**（マージ不要、上書きで済む）

### opencode

- `~/.config/opencode/plugins/*.ts|*.js` — 同じくディレクトリ自動検出
- API: `export const Name = async (ctx: {directory, $, ...}) => ({ event: async ({event}) => {...} })`
- 観測済みイベント（`resumex-hook.ts` / `adrafinil.ts` / `herdr-agent-state.js` から
  実証）: `session.created`（開始）, `session.idle`（ターン完了 = Stop相当）,
  `session.compacted`, `session.updated`, `session.status`,
  `tool.execute.before/after`
- ファイル所有モデル: ツールごとに専用ファイル1本

### grok

- `~/.grok/hooks/*.json` — Claude/Codex と同じ
  `{"hooks":{EventName:[{matcher?, hooks:[{type:"command",command,timeout}]}]}}` 形式
- 実在ファイル: `resumex.json`, `herdr.json` — **ツールごとに専用ファイル**
  （Claude/Codex のような単一共有設定ファイルへのマージではない）
- 確認済みイベント: `SessionStart`。ログ内 `hook_execution` イベント名
  （`LOCAL-FIRST-harness-matrix.md` §5 記載: `session_start`, `stop`,
  `user_prompt_submit`）から `Stop`/`UserPromptSubmit` 相当も存在が濃厚だが、
  `~/.grok/hooks/*.json` 経由で実際に登録できるかは**未実証**（§5-1）

**要更新**: `LOCAL-FIRST-harness-matrix.md` §3/4/5 の hooks 欄。

---

## 3. 書き込み先モデルによる分類

| モデル | harness | 特徴 |
|---|---|---|
| 共有設定ファイルへのマージ | claude(`settings.json`), codex(`hooks.json`) | 他の設定と同居。コマンド単位の重複チェックが必須 |
| 専用ファイル1本（自動検出dir） | omp-pi(`.ts`), opencode(`.ts`/`.js`), grok(`.json`) | 上書きで済む。マージ不要 |

grok は JSON 形式という点で claude/codex に似て見えるが、**ファイル所有モデル**
（専用ファイル1本 vs 共有ファイルへのマージ）で見ると実装コストは
omp-pi/opencode 側に近い。分類軸は「フォーマット」ではなく「ファイル所有モデル」に
すべき。

---

## 4. 提案するアーキテクチャ

### 4.1 lifecycle→command の正規定義（1箇所に集約）

```python
# adapters/harness/lifecycle.py（新規）
@dataclass(frozen=True)
class LifecycleCommands:
    on_turn_end: str                    # loci index --harness {harness}
    on_session_start: tuple[str, ...]   # server start / distill --limit N / prime
    on_compact: str                     # loci prime

def lifecycle_commands(harness: str, batch_limit: int) -> LifecycleCommands:
    loci = loci_bin()
    return LifecycleCommands(
        on_turn_end=f"{loci} index --harness {harness}",
        on_session_start=(
            f"nohup {loci} server start > /dev/null 2>&1 &",
            f"nohup {loci} distill --limit {batch_limit} > /dev/null 2>&1 &",
            f"{loci} prime",
        ),
        on_compact=f"{loci} prime",
    )
```

`ClaudeHooks`/`CodexHooks` はこの戻り値を「どう書き込むか」だけに専念するようリファクタする。

### 4.2 Writer を2系統に分割

`core/ports.py` の `Hooks` Protocol（`install`/`uninstall`/`fallback_recipe`）はそのまま維持。
実装を以下2つの共通baseに集約する。

```python
class MergedJsonHookWriter:
    """settings ファイルへコマンド単位でマージする（claude, codex, grok）"""
    settings_path: Path
    event_map: Mapping[str, tuple[str, str | None]]  # lifecycle key -> (native event, matcher)
    def install(self, project_root, commands: LifecycleCommands) -> tuple[bool, str]: ...

class DedicatedFileWriter:
    """専用ファイル1本を生成する（omp-pi, opencode）"""
    target_path: Path
    render: Callable[[LifecycleCommands], str]  # harness固有のテンプレート
    marker: str                                  # 冪等・アンインストール判定用
    def install(self, project_root, commands: LifecycleCommands) -> tuple[bool, str]: ...
```

grok は JSON かつ専用ファイルという中間的性質を持つため、`MergedJsonHookWriter` を
「対象ファイルが常に空から始まる（他ツールの設定と同居しない）」パラメータ付きで
再利用する（マージロジックは共通、ファイルパスだけ `~/.grok/hooks/codeatrium.json`
のように分離）。

`ClaudeHooks`/`CodexHooks`/新規`GrokHooks` は `MergedJsonHookWriter` の薄いラッパーに
縮退。新規 `OmpPiHooks`/`OpenCodeHooks` は `DedicatedFileWriter` のインスタンス化のみで
実装できる。

### 4.3 冪等性・アンインストール

専用ファイル系は resumex/herdr の既存実装が使っているマーカーコメント規約
（例: `// CODEATRIUM_HOOK_MARKER`、resumex の `RESUMEX_HOOK_MARKER` に倣う）を踏襲する。
再インストール時は上書き、アンインストール時はマーカー付きファイルを削除する。
共有マージ系（claude/codex/grok）は現行の「コマンド文字列一致でスキップ」ロジックを維持する。

### 4.4 蒸留モデル部分は変更しない

`model/registry.py` の client 選択（`ollama-ft`/`claude-cli`/`openai-compat`）は
**harness と直交**——`config.toml` の `[distill]` セクションのみで決まり、どの harness が
会話を生成したかとは無関係（`LOCAL-FIRST.md` §2.0 の core⊥harness⊥model 分離原則通り）。
ハーネスごとに変わるのは「いつ `loci distill` を呼ぶか（トリガー）」だけであり、
「何で蒸留するか（モデル）」はこの設計の対象外。

---

## 5. 未決事項

1. **grok**: `Stop`/`UserPromptSubmit` 相当イベントが `~/.grok/hooks/*.json` 経由で
   実際に登録可能か未検証。実装前に1件テスト登録して確認する
2. `DedicatedFileWriter` の uninstall 時、他ツール（resumex/herdr等）と共有する
   ディレクトリ内で誤って別ファイルを消さないためのマーカー規約をどこで一元定義するか
3. `loci init` が自動で omp-pi/opencode/grok にも hook を入れるべきか。
   claude=自動・codex=明示、の非対称が既にある。CLAUDE.mdの「未対応のケースを
   寛容なデフォルトで隠さない」原則に照らすと、**明示 `--harness` 指定を必須にする**
   （codex方式に統一）のが安全
4. 実装順序: matrix既存の推奨順（omp-pi→codex→opencode→grok）は LogSource の話。
   hooks だけで見るなら**証拠の確度が高い順**（omp-pi, opencode, grok の順 —
   いずれも本設計で実機確認済み）でよい

---

## 6. 非目標

- `FallbackHooks` クラス自体の削除（6つ目の未知 harness のための保険として維持）
- `loci hook install` の対話UXフロー変更（プロンプト文言等）
- 蒸留モデル(client)選択ロジックの変更（§4.4）
- `~/.omp/agent/extensions/codeatrium-hook.ts`（前回作業で手動設置済み）を
  この設計の正式な実装として扱うこと — あれは codeatrium 本体のコード生成を
  経ていない暫定パッチであり、`OmpPiHooks`（§4.2）実装後は `loci hook install
  --harness omp-pi` で正規に置き換える
