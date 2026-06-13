"""LLM 呼び出しラッパー: claude --print または OpenAI API でプロンプトを実行し JSON を返す

DistillBackend 抽象化により claude / openai プロバイダ両対応。
会話エッセンス (exchange_core, specific_context, room_assignments) を蒸留する"""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
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

# プロンプト sha256 先頭8桁（B5: drift 検出用）
DISTILL_PROMPT_VERSION = hashlib.sha256(DISTILL_PROMPT_TEMPLATE.encode()).hexdigest()[
    :8
]

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


# ---- 例外 ----


class LLMValidationError(Exception):
    """LLM response validation failed"""

    pass


# ---- DistillBackend 抽象化 ----


@dataclass(frozen=True)
class DistillBackend:
    """LLM backend configuration for distillation (claude or openai)"""

    provider: str
    model: str
    base_url: str | None

    @classmethod
    def from_config(cls, cfg) -> DistillBackend:
        """Create DistillBackend from Config object"""
        return DistillBackend(
            provider=cfg.distill_provider,
            model=cfg.distill_model,
            base_url=cfg.distill_base_url,
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


# ---- ヘルパー関数 ----


def _strip_json_fence(text: str) -> str:
    """Remove markdown code fence (```...```) if present, return raw string"""
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _build_distill_prompt(base_prompt: str) -> str:
    """Append JSON schema and instruction to base prompt"""
    return (
        base_prompt
        + "\n"
        + JSON_SCHEMA
        + "\n"
        + "このスキーマに厳密に従って、上記の指示通りにJSONのみで回答してください。"
    )


def _validate_palace(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate that raw dict has required palace object fields"""
    if not isinstance(raw.get("exchange_core"), str):
        raise LLMValidationError("exchange_core must be a string")
    if not isinstance(raw.get("specific_context"), str):
        raise LLMValidationError("specific_context must be a string")
    if not isinstance(raw.get("room_assignments"), list):
        raise LLMValidationError("room_assignments must be a list")

    for i, room in enumerate(raw.get("room_assignments", [])):
        if not isinstance(room, dict):
            raise LLMValidationError(f"room_assignments[{i}] must be a dict")
        required_keys = {"room_type", "room_key", "room_label", "relevance"}
        if not all(k in room for k in required_keys):
            raise LLMValidationError(
                f"room_assignments[{i}] missing required keys: {required_keys}"
            )

    return raw


def _call_claude_cli(prompt: str, model: str | None = None) -> dict[str, Any]:
    """Call claude --print CLI and return parsed JSON (unvalidated)"""
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
            text = _strip_json_fence(inner)
            return json.loads(text)
    return outer


def _call_openai(prompt: str, backend: DistillBackend) -> dict[str, Any]:
    """Call OpenAI API with retry on validation failure, return parsed JSON"""
    if backend.base_url is None:
        raise RuntimeError("base_url is required for openai provider")
    enhanced_prompt = _build_distill_prompt(prompt)

    body = {
        "model": backend.model,
        "messages": [{"role": "user", "content": enhanced_prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    body_bytes = json.dumps(body).encode()

    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url=backend.base_url + "/chat/completions",
                data=body_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as response:
                response_text = response.read().decode()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            raise RuntimeError(f"OpenAI API request failed: {e}") from e

        response_data = json.loads(response_text)
        message_content = response_data["choices"][0]["message"]["content"]
        stripped = _strip_json_fence(message_content)
        result = json.loads(stripped)

        try:
            return _validate_palace(result)
        except LLMValidationError:
            if attempt == 1:
                raise
    raise RuntimeError("retry exhausted")


# ---- LLM 呼び出し ----


def call_claude(
    prompt: str, model: str | None = None, *, backend: DistillBackend | None = None
) -> dict[str, Any]:
    """Dispatch to configured backend (claude CLI or OpenAI API) and return validated palace object"""
    if backend is None:
        from codeatrium.config import DEFAULT_DISTILL_MODEL

        backend = DistillBackend(
            provider="claude",
            model=model or DEFAULT_DISTILL_MODEL,
            base_url=None,
        )

    if backend.provider == "openai":
        raw = _call_openai(prompt, backend)
    else:
        raw = _call_claude_cli(prompt, backend.model)

    return _validate_palace(raw)
