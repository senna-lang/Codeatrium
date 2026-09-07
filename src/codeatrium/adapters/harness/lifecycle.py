"""lifecycle イベント → loci コマンドの正規定義（1箇所に集約）。

`ClaudeHooks`/`CodexHooks`/`GrokHooks`/`OmpPiHooks`/`OpenCodeHooks`
（`adapters/harness/hooks.py`、Claude は `codeatrium.hooks.install_hooks`
経由）は、どの harness でも「ターン終了時に index」「session 開始時に
server/distill/prime」「compact 時に prime」という同一の知識を持っており、
以前は harness ごとにコマンド文字列をリテラルに再構築していた（design doc §1）。
この重複を無くし、上記5つの Hooks 実装はここが返す `LifecycleCommands` を
「どう書き込むか」だけに専念して消費する。

`codeatrium.hooks.install_hooks`/`uninstall_hooks` は差分検出・自動修復を含む
Claude 固有の idempotency ロジックを保つが、コマンド文字列自体は
`lifecycle_commands("claude", batch_limit)` から取得する（実際の JSON への
書き込み・`Path.home` 解決は引き続き `codeatrium.hooks` 側の責務）。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from codeatrium.paths import loci_bin


@dataclass(frozen=True)
class LifecycleCommands:
    """harness に依存しない、lifecycle イベントごとの loci コマンド文字列。"""

    on_turn_end: str
    """ターン終了（Stop 相当）で実行するコマンド: `loci index --harness {harness}`"""

    on_session_start: tuple[str, str, str]
    """session 開始で実行する (server start, distill, prime) の3コマンド。
    server/distill は nohup で detach、prime のみ foreground（stdout をコンテキストへ注入）。"""

    on_compact: str
    """compact 完了（PostCompact 相当）で実行するコマンド: `loci prime`"""


def lifecycle_commands(harness: str, batch_limit: int) -> LifecycleCommands:
    """harness と蒸留バッチ上限から、lifecycle イベントごとの loci コマンドを組み立てる。"""
    loci = shlex.quote(loci_bin())
    return LifecycleCommands(
        on_turn_end=f"{loci} index --harness {harness}",
        on_session_start=(
            f"nohup {loci} server start > /dev/null 2>&1 &",
            f"nohup {loci} distill --limit {int(batch_limit)} > /dev/null 2>&1 &",
            f"{loci} prime",
        ),
        on_compact=f"{loci} prime",
    )
