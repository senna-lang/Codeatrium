"""loci CLI エントリポイント — サブコマンド登録のみ"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer

from codeatrium.cli.distill_cmd import distill
from codeatrium.cli.eval_cmd import eval_app
from codeatrium.cli.hook_cmd import hook_app
from codeatrium.cli.index_cmd import index
from codeatrium.cli.prime_cmd import prime
from codeatrium.cli.search_cmd import context, search
from codeatrium.cli.server_cmd import server_app
from codeatrium.cli.show_cmd import dump, show
from codeatrium.cli.status_cmd import status

app = typer.Typer(help="CLI-first memory layer for AI coding agents")

DEFAULT_DISTILL_RECENT = 50

_BANNER = r"""░█▀▀░█▀█░█▀▄░█▀▀░█▀█░▀█▀░█▀▄░▀█▀░█░█░█▄█
░█░░░█░█░█░█░█▀▀░█▀█░░█░░█▀▄░░█░░█░█░█░█
░▀▀▀░▀▀▀░▀▀░░▀▀▀░▀░▀░░▀░░▀░▀░▀▀▀░▀▀▀░▀░▀"""

_GRADIENT_BLUE = ["#7bb8ff", "#4a9eff", "#1b45a8"]


def _print_banner() -> None:
    """init コマンド冒頭のバナーを Panel で表示する"""
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    from codeatrium import __version__

    console = Console(soft_wrap=True)

    content = Text()
    for line, color in zip(_BANNER.split("\n"), _GRADIENT_BLUE, strict=True):
        content.append(line + "\n", style=f"bold {color}")
    content.append("\n")
    content.append("● ", style="bright_cyan")
    content.append("memory palace for AI coding agents", style="dim")
    content.append("   ")
    content.append(f"v{__version__}", style="dim cyan")

    console.print(
        Panel(content, border_style="blue", padding=(1, 3), expand=False)
    )


def _cleanup_partial_codeatrium_dir(codeatrium_dir: Path, dir_preexisted: bool) -> None:
    """init 実行フェーズが失敗した際、今回新規作成した .codeatrium/ のみ掃除する。
    ユーザーが事前に作成していたディレクトリ（dir_preexisted=True）は削除しない。
    """
    if not dir_preexisted:
        shutil.rmtree(codeatrium_dir, ignore_errors=True)


@app.command()
def init(
    skip_existing: Annotated[
        bool,
        typer.Option("--skip-existing", help="既存 exchange の蒸留をスキップする"),
    ] = False,
    distill_limit: Annotated[
        int | None,
        typer.Option("--distill-limit", help="既存 exchange のうち直近 N 件のみ蒸留対象にする"),
    ] = None,
    min_chars: Annotated[
        int | None,
        typer.Option("--min-chars", help="既存 exchange の最小文字数フィルタ（省略時は対話で選択）"),
    ] = None,
    no_hooks: Annotated[
        bool,
        typer.Option("--no-hooks", help="Claude Code の hook 自動登録をスキップ"),
    ] = False,
    no_local_distiller: Annotated[
        bool,
        typer.Option(
            "--no-local-distiller",
            help="ローカル蒸留モデルの pull 提案をスキップする",
        ),
    ] = False,
    distill_client: Annotated[
        str | None,
        typer.Option(
            "--distill-client",
            help="蒸留 client を明示指定する（ollama-ft | claude-cli）。Ready でなければエラー終了",
        ),
    ] = None,
) -> None:
    """プロジェクトルートに .codeatrium/memory.db を初期化する"""
    from codeatrium.db import get_connection, init_db
    from codeatrium.indexer import index_file, parse_exchanges
    from codeatrium.paths import (
        db_path,
        find_project_root,
        resolve_claude_projects_path,
    )

    _print_banner()

    root = find_project_root()
    db = db_path(root)

    if db.exists():
        typer.echo(f"Already initialized: {db}")
        return

    # 既存セッションの検出
    target_dir = resolve_claude_projects_path(root)
    jsonl_files = list(target_dir.rglob("*.jsonl")) if target_dir else []

    # --- 対話フェーズ（DB 作成前にすべての質問を完了する） ---
    resolved_min_chars = 50
    skip_count = 0
    skip_strategy = "recent"
    total_exchanges = 0
    run_distill_now = False

    if jsonl_files:
        resolved_min_chars = _resolve_min_chars(jsonl_files, min_chars)

        total_exchanges = sum(
            len(parse_exchanges(jsonl, min_chars=resolved_min_chars))
            for jsonl in jsonl_files
        )

        if total_exchanges > 0:
            skip_count, skip_strategy = _resolve_skip_count(
                total_exchanges, skip_existing, distill_limit
            )

            distill_count = total_exchanges - skip_count
            if distill_count > 0:
                run_distill_now = _ask_run_distill_now(distill_count)

    chosen_client = _resolve_init_distill_client(
        root,
        no_local_distiller=no_local_distiller,
        distill_client_flag=distill_client,
    )

    # --- 実行フェーズ（ここから DB・ファイルを作成） ---
    # 失敗時は作成した .codeatrium/ を掃除して次回再実行できる状態に戻す
    codeatrium_dir = db.parent
    dir_preexisted = codeatrium_dir.exists()
    try:
        init_db(db)

        config_path = codeatrium_dir / "config.toml"
        if not config_path.exists():
            if chosen_client is not None:
                from codeatrium.adapters.model.registry import write_client_config

                write_client_config(config_path, chosen_client)
                # write_client_config は既定で [distill]/[index] のみ生成する。
                # init 由来のコメント（batch_limit/min_chars 案内）を後段に追記する。
                config_path.write_text(
                    config_path.read_text().rstrip("\n")
                    + "\n"
                    + "# batch_limit = 20\n"
                    + "# min_chars = 100   # この文字数未満の exchange は蒸留スキップ\n"
                )
            else:
                config_path.write_text(
                    "# Codeatrium configuration\n"
                    "\n"
                    "[distill]\n"
                    "# distill client が未設定です。次のコマンドで選択してください:\n"
                    "#   loci distill --setup\n"
                    "#\n"
                    '# client = "ollama-ft"     # ローカル FT モデル（Ollama）\n'
                    '# client = "claude-cli"    # Claude CLI (claude --print)\n'
                    '# model = "..."\n'
                    '# base_url = "..."         # ollama-ft / openai-compat のみ\n'
                    "# batch_limit = 20\n"
                    "# min_chars = 100   # この文字数未満の exchange は蒸留スキップ\n"
                    "\n"
                    "[index]\n"
                    "# min_chars = 50   # trivial フィルタ閾値（文字数）\n"
                )

        typer.echo(f"Initialized: {db}")
    except KeyboardInterrupt:
        typer.echo(
            "\n⚠ Interrupted. Cleaning up partial state...", err=True
        )
        _cleanup_partial_codeatrium_dir(codeatrium_dir, dir_preexisted)
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"\n⚠ init failed: {exc}", err=True)
        typer.echo("Cleaning up partial state...", err=True)
        _cleanup_partial_codeatrium_dir(codeatrium_dir, dir_preexisted)
        raise typer.Exit(code=1) from None

    # --- AGENTS.md 注入（.codeatrium/ の作成成否とは独立した副作用なので、
    #     失敗しても既に作成済みの DB/config を rmtree で巻き込まない。#17）---
    from codeatrium.cli.prime_cmd import inject_agents_md

    try:
        if inject_agents_md(root):
            typer.echo(f"Updated: {root / 'AGENTS.md'} (codeatrium section)")
    except KeyboardInterrupt:
        typer.echo(
            "\n⚠ Interrupted while updating AGENTS.md. "
            "The database was created successfully.",
            err=True,
        )
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"\n⚠ AGENTS.md update failed: {exc}\n"
            "The database was created successfully — fix AGENTS.md manually.",
            err=True,
        )

    # --- 既存 exchange のインデックス化（失敗時は今回作成分の .codeatrium/ を掃除） ---
    try:
        if total_exchanges > 0:
            actual_total = 0
            for jsonl in jsonl_files:
                try:
                    actual_total += index_file(
                        jsonl, db, min_chars=resolved_min_chars
                    )
                except Exception as exc:  # noqa: BLE001
                    typer.echo(f"  ⚠ skip {jsonl.name}: {exc}", err=True)

            typer.echo(f"Indexed {actual_total} existing exchange(s).")

            if skip_count > 0:
                order_clause = (
                    "ORDER BY LENGTH(user_content) + LENGTH(agent_content) ASC"
                    if skip_strategy == "longest"
                    else "ORDER BY ply_start ASC"
                )
                con = get_connection(db)
                try:
                    con.execute(
                        f"""
                        UPDATE exchanges SET distilled_at = 'skipped', distill_status = 'skipped'
                        WHERE distilled_at IS NULL
                        AND id IN (
                            SELECT id FROM exchanges
                            WHERE distilled_at IS NULL
                            {order_clause}
                            LIMIT ?
                        )
                        """,
                        (skip_count,),
                    )
                    con.commit()
                finally:
                    con.close()
                remaining = actual_total - skip_count
                typer.echo(
                    f"Marked {skip_count} exchange(s) as skipped. "
                    f"{remaining} will be distilled."
                )
            else:
                typer.echo(f"All {actual_total} exchange(s) will be distilled.")
    except KeyboardInterrupt:
        typer.echo(
            "\n⚠ Interrupted. Cleaning up partial state...", err=True
        )
        _cleanup_partial_codeatrium_dir(codeatrium_dir, dir_preexisted)
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"\n⚠ init failed: {exc}", err=True)
        typer.echo("Cleaning up partial state...", err=True)
        _cleanup_partial_codeatrium_dir(codeatrium_dir, dir_preexisted)
        raise typer.Exit(code=1) from None

    # --- Hook 自動登録（opt-out 可、失敗は警告のみで続行） ---
    if not no_hooks:
        try:
            from codeatrium.config import load_config
            from codeatrium.hooks import install_hooks

            cfg = load_config(root)
            _changed, message = install_hooks(batch_limit=cfg.distill_batch_limit)
            typer.echo(message)
        except Exception as exc:  # noqa: BLE001
            typer.echo(
                f"\n⚠ Hook install failed: {exc}\n"
                "Retry later with: loci hook install",
                err=True,
            )

    # --- 蒸留フェーズ（失敗しても DB は残す: 後で loci distill で再試行可） ---
    if run_distill_now:
        from codeatrium.embedder import EmbedderSetupError
        from codeatrium.llm import DistillUnconfiguredError

        try:
            from codeatrium.config import load_config
            from codeatrium.distiller import distill_all
            from codeatrium.llm import DistillBackend

            cfg = load_config(root)
            typer.echo("Running distillation...")

            def _on_progress(
                cur: int, tot: int, error: str | None = None
            ) -> None:
                if error:
                    typer.echo(f"  [{cur}/{tot}] error: {error}", err=True)
                else:
                    typer.echo(f"  [{cur}/{tot}] distilled", err=True)

            count, err_count = distill_all(
                db,
                backend=DistillBackend.from_config(cfg),
                on_progress=_on_progress,
                project_root=str(root),
                distill_min_chars=cfg.distill_min_chars,
            )
            typer.echo(f"Distilled {count} exchange(s).")
            if err_count > 0:
                typer.echo(f"{err_count} exchange(s) failed — see errors above.", err=True)
        except KeyboardInterrupt:
            typer.echo(
                "\n⚠ Distillation interrupted. "
                "Indexed exchanges remain — resume with: loci distill",
                err=True,
            )
            raise typer.Exit(code=130) from None
        except DistillUnconfiguredError:
            typer.echo(
                "\nDistill client is not configured — skipping distillation.\n"
                "Configure and retry with: loci distill --setup",
                err=True,
            )
        except EmbedderSetupError as exc:
            typer.echo(
                f"\n⚠ {exc}\n"
                "Indexed exchanges remain — retry with: loci distill "
                "after fixing the environment.",
                err=True,
            )
            raise typer.Exit(code=1) from None
        except Exception as exc:  # noqa: BLE001
            typer.echo(
                f"\n⚠ Distillation failed: {exc}\n"
                "Indexed exchanges remain — retry with: loci distill",
                err=True,
            )
            raise typer.Exit(code=1) from None


_MIN_CHARS_CANDIDATES = [50, 100, 200, 500]


def _count_exchanges_by_threshold(
    jsonl_files: list[Path], thresholds: list[int]
) -> dict[int, int]:
    """各閾値ごとの exchange 件数をカウントする。min_chars=0 で全件パースして集計。"""
    from codeatrium.indexer import parse_exchanges

    # 全 exchange の combined 文字数を収集
    lengths: list[int] = []
    for jsonl in jsonl_files:
        for ex in parse_exchanges(jsonl, min_chars=0):
            lengths.append(len(ex.user_content) + len(ex.agent_content))

    return {t: sum(1 for length in lengths if length >= t) for t in thresholds}


def _prompt_choice(valid: set[str], default: str = "1") -> str:
    """有効な値が入力されるまで再プロンプトする"""
    sorted_valid = sorted(valid, key=lambda s: (len(s), s))
    while True:
        choice = typer.prompt("Choice", default=default).strip()
        if choice in valid:
            return choice
        typer.echo(
            f"  Invalid choice. Please enter one of: {', '.join(sorted_valid)}"
        )


def _prompt_int_range(prompt: str, min_v: int, max_v: int | None = None) -> int:
    """範囲制約付きの整数プロンプト。範囲外は再入力。"""
    while True:
        n = typer.prompt(prompt, type=int)
        if n < min_v:
            typer.echo(f"  Must be ≥ {min_v}.")
            continue
        if max_v is not None and n > max_v:
            typer.echo(f"  Must be ≤ {max_v}.")
            continue
        return n


def _resolve_min_chars(
    jsonl_files: list[Path], min_chars_flag: int | None
) -> int:
    """init 時の min_chars を決定する。フラグ指定済みならそのまま、未指定なら対話。"""
    if min_chars_flag is not None:
        return min_chars_flag

    counts = _count_exchanges_by_threshold(jsonl_files, _MIN_CHARS_CANDIDATES)

    # exchange が0件なら対話不要
    if counts.get(_MIN_CHARS_CANDIDATES[0], 0) == 0:
        return _MIN_CHARS_CANDIDATES[0]

    typer.echo("\nMin chars threshold for existing exchanges:")
    for i, threshold in enumerate(_MIN_CHARS_CANDIDATES, 1):
        label = " (default)" if threshold == 50 else ""
        typer.echo(f"  [{i}] {threshold} chars{label} — {counts[threshold]} exchanges")
    custom_idx = len(_MIN_CHARS_CANDIDATES) + 1
    typer.echo(f"  [{custom_idx}] Custom")

    valid = {str(i) for i in range(1, custom_idx + 1)}
    choice = _prompt_choice(valid)

    for i, threshold in enumerate(_MIN_CHARS_CANDIDATES, 1):
        if choice == str(i):
            return threshold

    # Custom
    return _prompt_int_range("Min chars threshold?", min_v=0)


def _ask_run_distill_now(distill_count: int) -> bool:
    """蒸留を今すぐ実行するか聞く。y/n/1/2 を受け付ける。"""
    typer.echo(
        f"\nStart distillation now? ({distill_count} exchanges, uses claude --print)"
    )
    typer.echo("  [1] No — distill on next session start (default)")
    typer.echo("  [2] Yes — run now")

    while True:
        choice = typer.prompt("Choice", default="1").strip().lower()
        if choice in ("1", "n", "no"):
            return False
        if choice in ("2", "y", "yes"):
            return True
        typer.echo("  Invalid choice. Please enter 1/2/y/n.")


def _resolve_init_distill_client(
    root: Path, *, no_local_distiller: bool, distill_client_flag: str | None
):
    """discover → (optional) setup offer → Ready 一覧 → 選択。

    Returns: 選ばれた ModelClient、または unconfigured を意味する None。
    `--distill-client` 明示指定時は Ready でなければエラー終了する（別 client に落とさない）。
    """
    from codeatrium.adapters.model.registry import (
        check_ready,
        discover,
        ready_clients,
        recommended_id,
        resolve_client,
        setup,
    )
    from codeatrium.config import load_config

    if distill_client_flag is not None:
        if distill_client_flag not in ("ollama-ft", "claude-cli"):
            typer.echo(f"Unknown distill client: {distill_client_flag}", err=True)
            raise typer.Exit(code=1)
        status = check_ready(distill_client_flag)
        if status.state != "ready" or status.client is None:
            typer.echo(
                f"--distill-client {distill_client_flag} is not ready: "
                f"{status.reason}",
                err=True,
            )
            raise typer.Exit(code=1)
        return status.client

    statuses = discover()
    for s in statuses:
        if s.state == "setupable" and not no_local_distiller:
            typer.echo(f"\n{s.label}: {s.reason}")
            if typer.confirm(f"Set up {s.label} now?", default=False):
                ok, msg = setup(s.id)
                typer.echo(msg)
                if ok:
                    statuses = discover()

    ready = ready_clients(statuses)
    if not ready:
        typer.echo(
            "\nNo distill client is ready. Leaving distill unconfigured — "
            "run `loci distill --setup` later."
        )
        return None

    rec = recommended_id(statuses)
    typer.echo("\nAvailable distill clients:")
    default_idx = 1
    for i, s in enumerate(ready, start=1):
        mark = " (recommended)" if s.id == rec else ""
        typer.echo(f"  {i}. {s.label} [{s.id}]{mark}")
        if s.id == rec:
            default_idx = i
    raw = typer.prompt("Select client", default=str(default_idx)).strip()
    idx = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(ready) else default_idx
    chosen = ready[idx - 1]
    return chosen.client or resolve_client(chosen.id, load_config(root))


def _ask_distill_priority() -> str:
    """蒸留対象の優先順位を選択する。"""
    typer.echo("\nDistill priority:")
    typer.echo("  [1] Recent — newest exchanges first")
    typer.echo("  [2] Longest — longest exchanges first")

    choice = _prompt_choice({"1", "2"})
    return "longest" if choice == "2" else "recent"


def _resolve_skip_count(
    total: int, skip_existing: bool, distill_limit: int | None
) -> tuple[int, str]:
    """スキップする exchange 数と優先順位を決定する。フラグ or 対話プロンプト。

    Returns: (skip_count, strategy) where strategy is "recent" or "longest"
    """
    if skip_existing:
        return total, "recent"
    if distill_limit is not None:
        skip = max(0, total - distill_limit)
        strategy = _ask_distill_priority() if skip > 0 else "recent"
        return skip, strategy

    # 対話プロンプト
    typer.echo(
        f"\nFound {total} existing exchanges from past sessions.\n"
        "Distillation uses an LLM "
        "(Claude Haiku by default, or a local model if configured) "
        "and may consume tokens.\n"
        "⚠ Skipped exchanges cannot be distilled later.\n"
    )
    typer.echo("How should existing exchanges be handled?")
    typer.echo("  [1] Skip all — only distill future sessions")
    typer.echo(f"  [2] Distill last {DEFAULT_DISTILL_RECENT} — recent history only")
    typer.echo(f"  [3] Distill all — {total} exchanges (token consumption)")
    typer.echo("  [4] Custom — specify how many exchanges to distill")

    choice = _prompt_choice({"1", "2", "3", "4"})

    if choice == "1":
        return total, "recent"
    if choice == "3":
        return 0, "recent"

    if choice == "2":
        skip = max(0, total - DEFAULT_DISTILL_RECENT)
    else:  # "4": Custom
        n = _prompt_int_range(
            "How many exchanges to distill?", min_v=1, max_v=total
        )
        skip = max(0, total - n)

    strategy = _ask_distill_priority() if skip > 0 else "recent"
    return skip, strategy


app.command()(index)
app.command()(distill)
app.command()(search)
app.command()(context)
app.command()(status)
app.command()(show)
app.command()(dump)
app.command()(prime)
app.add_typer(hook_app, name="hook")
app.add_typer(server_app, name="server")
app.add_typer(eval_app, name="eval")
