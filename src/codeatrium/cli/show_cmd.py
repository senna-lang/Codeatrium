"""loci show / loci dump コマンド"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer


def show(
    ref: Annotated[str, typer.Argument(help="verbatim_ref (path:ply=N)")],
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """verbatim_ref から exchange の原文を取得する"""
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    if ":ply=" not in ref:
        typer.echo("Invalid ref format. Expected: <path>:ply=<N>", err=True)
        raise typer.Exit(1)
    path_part, ply_part = ref.rsplit(":ply=", 1)
    try:
        ply = int(ply_part)
    except ValueError:
        typer.echo(f"Invalid ply value: {ply_part}", err=True)
        raise typer.Exit(1)

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    row = con.execute(
        """
        SELECT e.user_content, e.agent_content, e.ply_start, e.ply_end
        FROM exchanges e
        JOIN conversations c ON c.id = e.conversation_id
        WHERE c.source_path = ? AND e.ply_start = ?
        """,
        (path_part, ply),
    ).fetchone()
    con.close()

    if row is None:
        typer.echo("Exchange not found.")
        return

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "user_content": row["user_content"],
                    "agent_content": row["agent_content"],
                    "ply_start": row["ply_start"],
                    "ply_end": row["ply_end"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"[User] (ply {row['ply_start']}-{row['ply_end']})")
        typer.echo(row["user_content"])
        typer.echo("\n[Agent]")
        typer.echo(row["agent_content"])


def dump(
    distilled: Annotated[
        bool, typer.Option("--distilled", help="蒸留済み palace objects を出力")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="最大件数")] = 1000,
    json_output: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """蒸留済み palace objects を新しい順に出力する（セッション開始時の in-context ロード用）"""
    from codeatrium.db import get_connection
    from codeatrium.paths import db_path, find_project_root

    if not distilled:
        typer.echo("Use --distilled to dump palace objects.", err=True)
        raise typer.Exit(1)

    root = find_project_root()
    db = db_path(root)
    if not db.exists():
        typer.echo("Not initialized. Run `loci init` first.", err=True)
        raise typer.Exit(1)

    con = get_connection(db)
    rows = con.execute(
        """
        SELECT p.id, p.exchange_id, p.exchange_core, p.specific_context,
               e.distilled_at
        FROM palace_objects p
        JOIN exchanges e ON e.id = p.exchange_id
        ORDER BY e.distilled_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        typer.echo("No distilled objects found.")
        con.close()
        return

    palace_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(palace_ids))
    room_rows = con.execute(
        f"""
        SELECT palace_object_id, room_type, room_key, room_label
        FROM rooms
        WHERE palace_object_id IN ({placeholders})
        ORDER BY relevance DESC
        """,
        palace_ids,
    ).fetchall()
    con.close()

    rooms_map: dict[str, list[Any]] = {}
    for r in room_rows:
        rooms_map.setdefault(r["palace_object_id"], []).append(
            {
                "room_type": r["room_type"],
                "room_key": r["room_key"],
                "room_label": r["room_label"],
            }
        )

    if json_output:
        output = [
            {
                "exchange_core": r["exchange_core"],
                "specific_context": r["specific_context"],
                "rooms": rooms_map.get(r["id"], []),
                "date": (r["distilled_at"] or "")[:10],
            }
            for r in rows
        ]
        typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            date = (r["distilled_at"] or "")[:10]
            typer.echo(f"\n[{date}] {r['exchange_core']}")
            if r["specific_context"]:
                typer.echo(f"  {r['specific_context']}")
            for rm in rooms_map.get(r["id"], [])[:2]:
                typer.echo(f"  #{rm['room_key']}")
