"""
ファイル改名の追従（design §8.2、段2: git log --follow 相当）。

`loci context <file>` が現在のパスで問い合わせても、そのファイルが過去に改名
されていれば、記録済みの code_edges は旧パスのままで見つからない
（例: `src/logo/db.py` → `src/codeatrium/db.py`）。ここでは現在のパスから
git 履歴を逆にたどって旧パスの一覧（エイリアス）を求める。

`loci context` は読み取り専用コマンドであり、Stop hook の `loci index` と
同じ DB に WAL で同時アクセスされ得るため、問い合わせ経路からは書き込まない
（git 呼び出しは実測20ms程度でコストは小さい）。`file_renames` テーブルへの
書き込みは §8.2 段1（ハーネスの move_path、将来のアダプター実装）専用に空けておく。
読み取りはそのテーブルも見るため、段1が書いた行があればここでも使われる。

git 呼び出しはこのモジュールだけの責務にする。`code_touches.py`（ディスク
非依存の純関数群）にも `context_lookup.py`（sqlite3.Connection だけで完結、
design の意図的な分離）にも subprocess を持ち込まない。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess

_RENAME_LINE_RE = re.compile(r"^R\d+\t(.+)\t(.+)$")

_GIT_TIMEOUT_SECONDS = 5.0


def parse_rename_log(output: str) -> list[tuple[str, str]]:
    """`git log --follow --name-status --diff-filter=R --format=` の出力を
    `(old_path, new_path)` のリストへ変換する（純関数）。

    1コミットに複数のリネームが含まれる場合や、対象と無関係なリネームが
    紛れ込む形式ではないため、`R<類似度>\\t旧パス\\t新パス` の行だけを拾えばよい。
    """
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        m = _RENAME_LINE_RE.match(line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs


def _run_git_follow(project_root: str, file_path: str) -> str:
    """`git log --follow` を1回呼び出し、標準出力を返す。

    リポジトリでない・git が無い・タイムアウトなど、いかなる失敗でも例外を
    投げず空文字列を返す（§3.3: 分からなければ何も言わない。呼び出し側の
    既存カスケードをそのまま続けさせる）。
    """
    try:
        result = subprocess.run(
            [
                "git", "-c", "core.quotepath=false",
                "log", "--follow", "--name-status",
                "--diff-filter=R", "--format=", "--", file_path,
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _recorded_aliases(con: sqlite3.Connection, file_path: str) -> list[str]:
    """§8.2 段1（ハーネスの move_path）が書いた行を読む。ここでは書き込まない。"""
    rows = con.execute(
        "SELECT old_path FROM file_renames WHERE new_path = ?", (file_path,)
    ).fetchall()
    return [r["old_path"] for r in rows]


def resolve_aliases(con: sqlite3.Connection, project_root: str, file_path: str) -> list[str]:
    """`file_path`（現在のパス）の旧パス一覧を返す（design §8.2）。

    段1（`file_renames` にハーネスが記録済みの行）を先に見る。無ければ
    段2として `git log --follow` を都度呼び、改名履歴を平坦化した旧パス一覧
    を返す（キャッシュはしない——`loci context` は読み取り専用コマンドであり
    書き込むと Stop hook の `loci index` と WAL 上で競合し得るため。git 呼び出し
    は1コマンド20ms程度で許容範囲）。改名履歴が無い・git が使えない場合は
    空リスト——呼び出し側の symbol/file/directory カスケードは変わらず動く。
    """
    recorded = _recorded_aliases(con, file_path)
    if recorded:
        return recorded

    output = _run_git_follow(project_root, file_path)
    pairs = parse_rename_log(output)
    return sorted({old for old, _new in pairs})
