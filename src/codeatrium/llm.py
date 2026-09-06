"""LLM 呼び出しラッパー: claude --print または OpenAI API でプロンプトを実行し JSON を返す

DistillBackend 抽象化により claude / openai プロバイダ両対応。
会話エッセンス (exchange_core, specific_context, room_assignments) を蒸留する"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeatrium.json_utils import extract_json

# ホスト型 OpenAI 互換 API で使う API キーの env 変数。
# 未設定なら Authorization ヘッダを付けない（ローカル Ollama の無認証経路を保持）。
DISTILL_API_KEY_ENV = "CODEATRIUM_DISTILL_API_KEY"

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


class DistillUnconfiguredError(Exception):
    """distill client が未設定（init/`loci distill --setup` が必要）"""

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
        """Config から DistillBackend を作る。distill_client 経由で ModelClient registry を解決する。

        unconfigured（client/provider どちらも未設定）は暗黙解決せず例外にする — silent fallback 禁止。
        """
        if cfg.distill_unconfigured or not cfg.distill_client:
            raise DistillUnconfiguredError(
                "distill client is not configured. Run `loci distill --setup`."
            )
        from codeatrium.adapters.model.registry import resolve_client

        client = resolve_client(cfg.distill_client, cfg)
        return DistillBackend(
            provider=client.provider,
            model=client.model,
            base_url=client.base_url,
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

    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude -p produced non-JSON stdout: {e}") from e
    if isinstance(outer, dict):
        if "structured_output" in outer and outer["structured_output"]:
            return outer["structured_output"]
        inner = outer.get("result", "")
        if isinstance(inner, str) and inner.strip():
            text = _strip_json_fence(inner)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"claude -p 'result' field is not valid JSON: {e}"
                ) from e
    return outer


_MAX_VALIDATION_ATTEMPTS = 2
_MAX_NETWORK_ATTEMPTS = 3
_NETWORK_RETRY_BACKOFF_SECONDS = 1.0


def _is_retryable_network_error(exc: OSError) -> bool:
    """5xx またはタイムアウトのみ再試行対象。4xx 等の恒久的失敗はリトライしても無意味。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, TimeoutError)


def _post_chat_completion(url: str, body: bytes, headers: dict[str, str]) -> str:
    """chat/completions へ POST する。

    5xx/timeout は bounded backoff で自動再試行し、それ以外のネットワーク失敗
    （4xx 等の恒久的失敗）は再試行せず即 RuntimeError にラップする。
    """
    for attempt in range(_MAX_NETWORK_ATTEMPTS):
        try:
            req = urllib.request.Request(
                url=url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=300) as response:
                return response.read().decode()
        except (urllib.error.URLError, TimeoutError) as e:
            is_last_attempt = attempt == _MAX_NETWORK_ATTEMPTS - 1
            if is_last_attempt or not _is_retryable_network_error(e):
                raise RuntimeError(f"OpenAI API request failed: {e}") from e
            time.sleep(_NETWORK_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError("OpenAI API request failed: retry exhausted")


def _call_openai(prompt: str, backend: DistillBackend) -> dict[str, Any]:
    """Call OpenAI API, return validated palace object.

    2軸で再試行する:
    - ネットワーク: 5xx/timeout は bounded backoff で自動再試行 (_post_chat_completion)。
    - validation: スキーマ検証失敗時、同一 body の再送は決定論的バックエンドでは
      必ず同じ結果になるため無意味 (issue #15)。是正メッセージを追記し temperature を
      上げて出力を変化させたうえで再送する。
    """
    if backend.base_url is None:
        raise RuntimeError("base_url is required for openai provider")
    enhanced_prompt = _build_distill_prompt(prompt)

    # ホスト型 API はキー必須。env にキーがある時だけ Authorization を付ける
    # （未設定ならローカル Ollama 等の無認証経路を変えない）。
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(DISTILL_API_KEY_ENV)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = backend.base_url + "/chat/completions"
    last_validation_error: LLMValidationError | None = None

    for attempt in range(_MAX_VALIDATION_ATTEMPTS):
        content = enhanced_prompt
        temperature: float = 0
        if last_validation_error is not None:
            content += (
                "\n\n[retry] 前回の応答はスキーマ検証に失敗しました: "
                f"{last_validation_error}\n"
                "指摘を踏まえ、スキーマに厳密準拠したJSONのみを再生成してください。"
            )
            temperature = 0.2

        body_bytes = json.dumps(
            {
                "model": backend.model,
                "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"},
                "temperature": temperature,
            }
        ).encode()

        response_text = _post_chat_completion(url, body_bytes, headers)

        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError as e:
            # プロキシ経由の HTML エラーページ等、HTTP body が JSON ですらない場合。
            raise RuntimeError(f"OpenAI API returned a non-JSON response: {e}") from e

        try:
            message_content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            # Ollama 等の互換サーバは HTTP 200 でエラーボディ ({"error": ...}) を
            # 返すことがある。choices 不在/形状不正を恒久的失敗として RuntimeError に
            # 集約し、呼び出し側 (distill_all) が該当 exchange をスキップできるようにする。
            raise RuntimeError(
                f"OpenAI API returned an unexpected response shape: {e}"
            ) from e

        # フェンス/散文/balanced 抽出で頑健化（単純 strip では取りこぼす応答に対応）。
        try:
            result = json.loads(extract_json(message_content))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"OpenAI API response content is not valid JSON: {e}"
            ) from e

        try:
            return _validate_palace(result)
        except LLMValidationError as e:
            last_validation_error = e
            if attempt == _MAX_VALIDATION_ATTEMPTS - 1:
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
