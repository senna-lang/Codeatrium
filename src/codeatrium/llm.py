"""LLM 呼び出しラッパー: claude --print でプロンプトを実行し JSON を返す"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

# ---- プロンプト定数 ----

DISTILL_PROMPT_TEMPLATE = """\
この対話のやり取りをJSONに蒸留してください：

- "exchange_core": 1-2文。何が達成または決定されましたか？\
やり取り内の特定の用語を使用してください。\
テキストに存在しない詳細を捏造しないでください。\
やり取りがほぼ空の場合は、簡潔にその旨を述べてください。
- "specific_context": テキストからの具体的な詳細1つ：\
数値、エラーメッセージ、パラメータ名、またはファイルパス。\
テキストから正確にコピーしてください。プロジェクトパスは使用しないでください。
- "room_assignments": 1-3個の部屋。各部屋はこのやり取りが属するトピックです。\
{{"room_type": "<file|concept|workflow>", "room_key": "<識別子>",\
 "room_label": "<短いラベル>", "relevance": <0.0-1.0>}}\
部屋は関連するやり取りをグループ化するのに十分具体的なものにしてください\
（例：「errors」ではなく「retry_timeout」）。

"files_touched"は含めないでください。

やり取り (メッセージ {ply_start}-{ply_end}): {messages_text}

JSONのみで回答してください。"""

JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "exchange_core": {"type": "string", "maxLength": 300},
            "specific_context": {"type": "string", "maxLength": 200},
            "room_assignments": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "room_type": {
                            "type": "string",
                            "enum": ["file", "concept", "workflow"],
                        },
                        "room_key": {"type": "string"},
                        "room_label": {"type": "string"},
                        "relevance": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["room_type", "room_key", "room_label", "relevance"],
                },
            },
        },
        "required": ["exchange_core", "specific_context", "room_assignments"],
    }
)


# ---- 副作用制御 ----


def _session_dir() -> Path:
    """claude -p が書き出す JSONL のディレクトリ"""
    return Path.home() / ".claude" / "projects"


def _snapshot_jsonl(session_dir: Path) -> set[Path]:
    if not session_dir.exists():
        return set()
    return set(session_dir.rglob("*.jsonl"))


def _cleanup_side_effect_jsonls(session_dir: Path, before: set[Path]) -> None:
    """claude -p 呼び出しで生成された JSONL を削除する"""
    if not session_dir.exists():
        return
    after = set(session_dir.rglob("*.jsonl"))
    for p in after - before:
        try:
            p.unlink()
        except OSError:
            pass


# ---- LLM 呼び出し ----


def call_claude(prompt: str, model: str | None = None) -> dict[str, Any]:
    """claude -p でプロンプトを実行し JSON を返す（テストでモック対象）"""
    import shutil

    from codeatrium.config import DEFAULT_DISTILL_MODEL

    cli = shutil.which("claude")
    if cli is None:
        raise RuntimeError("claude CLI not found in PATH")

    session_dir = _session_dir()
    before = _snapshot_jsonl(session_dir)

    try:
        result = subprocess.run(
            [
                cli,
                "--print",
                "--model",
                model or DEFAULT_DISTILL_MODEL,
                "--output-format",
                "json",
                "--json-schema",
                JSON_SCHEMA,
                "--no-session-persistence",
                "--setting-sources",
                "",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        _cleanup_side_effect_jsonls(session_dir, before)

    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr}")

    outer = json.loads(result.stdout)
    if isinstance(outer, dict):
        if "structured_output" in outer and outer["structured_output"]:
            return outer["structured_output"]
        inner = outer.get("result", "")
        if isinstance(inner, str) and inner.strip():
            text = inner.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                )
            return json.loads(text.strip())
    return outer
