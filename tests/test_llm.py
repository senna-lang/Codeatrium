"""call_claude のユニットテスト: subprocess.run をモックして振る舞いを検証する"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeatrium.llm import (
    _MAX_NETWORK_ATTEMPTS,
    DistillBackend,
    LLMValidationError,
    _call_claude_cli,
    _call_openai,
    _strip_json_fence,
    _validate_palace,
    call_claude,
)

# ---- テストデータ ----


MOCK_JSON_RESPONSE = {
    "structured_output": {
        "exchange_core": "テスト交換: パラメータを設定した",
        "specific_context": "timeout=300",
        "room_assignments": [
            {
                "room_type": "concept",
                "room_key": "test-param",
                "room_label": "Test Parameter",
                "relevance": 0.85,
            }
        ],
    }
}


# ---- テスト ----


def test_call_claude_command_args() -> None:
    """
    subprocess.run をモックし、call_claude 実行時のコマンドリストに
    --no-session-persistence, --session-id, --setting-sources,
    --output-format (json), --model が含まれることを assert する
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)

    with patch("codeatrium.llm.subprocess.run", return_value=mock_result) as mock_run:
        with patch("shutil.which", return_value="/usr/bin/claude"):
            call_claude("test prompt")

            # subprocess.run が呼ばれたことを確認
            assert mock_run.called

            # 呼び出し時のコマンドリスト取得
            call_args = mock_run.call_args
            assert call_args is not None
            cmd_list = call_args[0][0]  # 第一引数のコマンドリスト

            # 必須フラグが含まれていることを確認
            assert "--no-session-persistence" in cmd_list
            assert "--session-id" in cmd_list
            assert "--setting-sources" in cmd_list
            assert "--output-format" in cmd_list
            assert "--model" in cmd_list
            assert "json" in cmd_list

            # claude コマンドパスと --print フラグ
            assert cmd_list[0] == "/usr/bin/claude"
            assert "--print" in cmd_list


def test_call_claude_returns_dict() -> None:
    """
    モックして call_claude の戻り値が期待する dict
    (structured_output を含む) であることを assert する
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)

    with patch("codeatrium.llm.subprocess.run", return_value=mock_result):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            result = call_claude("test prompt")

            # 戻り値が dict で、期待するキーを含むことを確認
            assert isinstance(result, dict)
            assert "exchange_core" in result
            assert "specific_context" in result
            assert "room_assignments" in result

            # 値の確認
            assert result["exchange_core"] == "テスト交換: パラメータを設定した"
            assert result["specific_context"] == "timeout=300"
            assert isinstance(result["room_assignments"], list)
            assert len(result["room_assignments"]) == 1


def test_call_claude_cleanup_on_success(tmp_path: Path) -> None:
    """
    _session_dir を tmp_path に向け、call_claude が割り当てた --session-id の
    JSONL が正常終了時にクリーンアップされることを確認する
    """
    captured: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        # 呼び出しに割り当てられた --session-id を取得し、そのファイル名で副作用を再現
        session_id = cmd[cmd.index("--session-id") + 1]
        captured["session_id"] = session_id
        (tmp_path / f"{session_id}.jsonl").write_text('{"key": "value"}\n')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)
        return mock_result

    with patch("codeatrium.llm.subprocess.run", side_effect=fake_run):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("codeatrium.llm._session_dir", return_value=tmp_path):
                call_claude("test prompt")

    # 正常終了後は自分自身の session_id の .jsonl がクリーンアップされたことを確認
    assert not (tmp_path / f"{captured['session_id']}.jsonl").exists()


def test_call_claude_cleanup_on_timeout(tmp_path: Path) -> None:
    """
    subprocess.run が subprocess.TimeoutExpired を投げるようモックし:
    (1) call_claude が例外を送出する (pytest.raises)
    (2) それでも自分自身の session_id の .jsonl クリーンアップが走る (finally 経路) ことを確認する
    """
    captured: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        session_id = cmd[cmd.index("--session-id") + 1]
        captured["session_id"] = session_id
        (tmp_path / f"{session_id}.jsonl").write_text('{"key": "value"}\n')
        raise subprocess.TimeoutExpired("claude", 300)

    with patch(
        "codeatrium.llm.subprocess.run",
        side_effect=fake_run,
    ):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("codeatrium.llm._session_dir", return_value=tmp_path):
                # TimeoutExpired が発生することを確認
                with pytest.raises(subprocess.TimeoutExpired):
                    call_claude("test prompt")

    # タイムアウト時にも自分自身の session_id の .jsonl がクリーンアップされたことを確認
    assert not (tmp_path / f"{captured['session_id']}.jsonl").exists()


def test_call_claude_cleanup_preserves_concurrent_session_jsonl(
    tmp_path: Path,
) -> None:
    """
    issue #14 の回帰テスト: claude -p 呼び出し中に無関係な並行セッションの JSONL が
    新規作成されても、cleanup はそれを削除してはならない
    （session_id が一致する自分自身のファイルのみ削除する）。
    """
    pre_existing_other_session = tmp_path / "pre-existing-other-session.jsonl"
    pre_existing_other_session.write_text('{"other": "already there"}\n')

    captured: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        session_id = cmd[cmd.index("--session-id") + 1]
        captured["session_id"] = session_id
        # 自分自身の副作用ファイル
        (tmp_path / f"{session_id}.jsonl").write_text('{"key": "value"}\n')
        # 呼び出しウィンドウ中に別プロセスが新規作成した「並行する無関係なセッション」
        new_concurrent_session = tmp_path / "new-concurrent-session.jsonl"
        new_concurrent_session.write_text('{"other": "concurrent user session"}\n')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)
        return mock_result

    with patch("codeatrium.llm.subprocess.run", side_effect=fake_run):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("codeatrium.llm._session_dir", return_value=tmp_path):
                call_claude("test prompt")

    # 自分自身のファイルは削除される
    assert not (tmp_path / f"{captured['session_id']}.jsonl").exists()
    # 並行する無関係なセッションのファイルは温存される（既存 / 新規いずれも）
    assert pre_existing_other_session.exists()
    assert (tmp_path / "new-concurrent-session.jsonl").exists()


def test_call_claude_dispatches_to_openai_backend() -> None:
    """
    call_claude が backend パラメータを受け取り、_call_openai に委譲することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="llama3", base_url="http://localhost:11434/v1"
    )

    with patch("codeatrium.llm._call_openai") as mock_call_openai:
        mock_call_openai.return_value = MOCK_JSON_RESPONSE["structured_output"]
        call_claude("prompt", backend=backend)

        # _call_openai が呼ばれ、prompt と backend が渡されたことを確認
        assert mock_call_openai.called
        call_args = mock_call_openai.call_args
        assert call_args is not None
        assert call_args[0][0] == "prompt"
        assert call_args[0][1] == backend


def test_call_claude_dispatches_to_claude_by_default() -> None:
    """
    call_claude が backend パラメータなしの場合、_call_claude_cli に委譲することを確認する
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)

    with patch("codeatrium.llm._call_claude_cli") as mock_call_claude_cli:
        mock_call_claude_cli.return_value = MOCK_JSON_RESPONSE["structured_output"]
        call_claude("prompt")

        # _call_claude_cli が呼ばれたことを確認
        assert mock_call_claude_cli.called


def test_call_openai_sends_correct_url_and_body() -> None:
    """
    _call_openai が正しい URL とボディで request を送信することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="llama3", base_url="http://localhost:11434/v1"
    )

    mock_response = MagicMock()
    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(MOCK_JSON_RESPONSE["structured_output"])
                    }
                }
            ]
        }
    )
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = response_body.encode()

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = _call_openai("prompt", backend)

        # urlopen が呼ばれたことを確認
        assert mock_urlopen.called

        # Request オブジェクトを取得
        call_args = mock_urlopen.call_args
        assert call_args is not None
        request_obj = call_args[0][0]

        # URL が正しいことを確認
        assert request_obj.full_url == "http://localhost:11434/v1/chat/completions"

        # Authorization ヘッダーがないことを確認
        assert "Authorization" not in request_obj.headers
        assert result == MOCK_JSON_RESPONSE["structured_output"]


def test_call_openai_no_authorization_header() -> None:
    """
    _call_openai の Request に Authorization ヘッダーがないことを確認する
    """
    backend = DistillBackend(
        provider="openai", model="llama3", base_url="http://localhost:11434/v1"
    )

    mock_response = MagicMock()
    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(MOCK_JSON_RESPONSE["structured_output"])
                    }
                }
            ]
        }
    )
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = response_body.encode()

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        _call_openai("prompt", backend)

        # Request オブジェクトを取得
        call_args = mock_urlopen.call_args
        assert call_args is not None
        request_obj = call_args[0][0]

        # Authorization ヘッダーがないことを確認
        assert "Authorization" not in request_obj.headers


def _mock_openai_response(structured: dict[str, Any], wrap: str = "plain") -> MagicMock:
    """choices[0].message.content に structured を様々な包み方で入れた応答モックを返す。"""
    content = json.dumps(structured)
    if wrap == "fence":
        content = f"```json\n{content}\n```"
    elif wrap == "prose":
        content = (
            f"以下が結果です:\n```json\n{content}\n```\nスキーマに準拠しています。"
        )
    body = json.dumps({"choices": [{"message": {"content": content}}]})
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = body.encode()
    return mock_response


def test_call_openai_adds_authorization_when_key_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEATRIUM_DISTILL_API_KEY が設定されていれば Bearer トークンを付ける。"""
    from codeatrium.llm import DISTILL_API_KEY_ENV

    monkeypatch.setenv(DISTILL_API_KEY_ENV, "sk-test-123")
    backend = DistillBackend(
        provider="openai", model="deepseek-chat", base_url="https://api.deepseek.com"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"])

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        _call_openai("prompt", backend)
        request_obj = mock_urlopen.call_args[0][0]
        # urllib は header 名を capitalize して保持する
        assert request_obj.headers.get("Authorization") == "Bearer sk-test-123"


def test_call_openai_no_authorization_when_key_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """キー env が無ければ Authorization を付けない（Ollama 無認証経路を保持）。"""
    from codeatrium.llm import DISTILL_API_KEY_ENV

    monkeypatch.delenv(DISTILL_API_KEY_ENV, raising=False)
    backend = DistillBackend(
        provider="openai", model="llama3", base_url="http://localhost:11434/v1"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"])

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        _call_openai("prompt", backend)
        request_obj = mock_urlopen.call_args[0][0]
        assert "Authorization" not in request_obj.headers


@pytest.mark.parametrize("wrap", ["plain", "fence", "prose"])
def test_call_openai_parses_wrapped_json(
    monkeypatch: pytest.MonkeyPatch, wrap: str
) -> None:
    """フェンス/散文で包まれた応答でも extract_json で本体を取り出してパースできる。"""
    from codeatrium.llm import DISTILL_API_KEY_ENV

    monkeypatch.delenv(DISTILL_API_KEY_ENV, raising=False)
    backend = DistillBackend(
        provider="openai", model="deepseek-chat", base_url="https://api.deepseek.com"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"], wrap)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _call_openai("prompt", backend)
        assert result == MOCK_JSON_RESPONSE["structured_output"]


def test_strip_json_fence_with_fence() -> None:
    """
    _strip_json_fence が markdown code fence を削除することを確認する
    """
    input_text = '```json\n{"a":1}\n```'
    result = _strip_json_fence(input_text)
    assert result == '{"a":1}'


def test_strip_json_fence_without_fence() -> None:
    """
    _strip_json_fence が fence なし JSON を返すことを確認する
    """
    input_text = '{"a":1}'
    result = _strip_json_fence(input_text)
    assert result == '{"a":1}'


def test_validate_palace_valid() -> None:
    """
    _validate_palace が有効な palace dict を受け入れることを確認する
    """
    palace = {
        "exchange_core": "c",
        "specific_context": "s",
        "room_assignments": [
            {
                "room_type": "concept",
                "room_key": "k",
                "room_label": "l",
                "relevance": 0.5,
            }
        ],
    }
    result = _validate_palace(palace)
    assert result == palace


def test_validate_palace_missing_key_raises() -> None:
    """
    _validate_palace が必須キーなしの dict で LLMValidationError を発生させることを確認する
    """
    palace = {"exchange_core": "c"}
    with pytest.raises(LLMValidationError):
        _validate_palace(palace)


def test_call_openai_retry_on_validation_failure() -> None:
    """
    _call_openai が validation 失敗時にリトライし、2回目に成功することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="llama3", base_url="http://localhost:11434/v1"
    )

    mock_response = MagicMock()
    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(MOCK_JSON_RESPONSE["structured_output"])
                    }
                }
            ]
        }
    )
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = response_body.encode()

    call_count = {"count": 0}

    def validate_side_effect(palace: dict[str, Any]) -> dict[str, Any]:
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise LLMValidationError("validation failed")
        return palace

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        with patch(
            "codeatrium.llm._validate_palace", side_effect=validate_side_effect
        ) as mock_validate:
            result = _call_openai("prompt", backend)

            # urlopen が2回呼ばれたことを確認
            assert mock_urlopen.call_count == 2

            # _validate_palace が2回呼ばれたことを確認
            assert mock_validate.call_count == 2

            # 結果が返されたことを確認
            assert result == MOCK_JSON_RESPONSE["structured_output"]


def test_call_openai_raises_after_two_validation_failures() -> None:
    """
    _call_openai が validation が2回失敗した場合、LLMValidationError を発生させることを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )

    mock_response = MagicMock()
    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(MOCK_JSON_RESPONSE["structured_output"])
                    }
                }
            ]
        }
    )
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = response_body.encode()

    with patch("urllib.request.urlopen", return_value=mock_response):
        with patch(
            "codeatrium.llm._validate_palace",
            side_effect=LLMValidationError("always fails"),
        ):
            with pytest.raises(LLMValidationError):
                _call_openai("prompt", backend)


def test_call_openai_raises_on_connection_error() -> None:
    """
    _call_openai が urllib.error.URLError を RuntimeError に変換することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        with pytest.raises(RuntimeError):
            _call_openai("prompt", backend)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost:11434/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def test_call_openai_missing_choices_raises_runtime_error() -> None:
    """
    Ollama 等の互換サーバが HTTP 200 でエラーボディ ({"error": ...}) を返した場合、
    無ガードの choices[0] アクセスで KeyError が漏れず RuntimeError にラップされることを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = json.dumps(
        {"error": {"message": "model not found"}}
    ).encode()

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError, match="unexpected response shape"):
            _call_openai("prompt", backend)


def test_call_openai_non_json_http_body_raises_distinct_runtime_error() -> None:
    """
    HTTP body 自体が JSON ですらない場合 (プロキシの HTML エラーページ等)、
    "unexpected response shape" ではなく "non-JSON response" として区別されることを確認する
    (choices 欠如などの「JSON として妥当だが形状不正」なケースとメッセージを分離する)
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = b"<html><body>502 Bad Gateway</body></html>"

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError, match="non-JSON response"):
            _call_openai("prompt", backend)


def test_call_openai_non_json_content_raises_runtime_error() -> None:
    """
    message.content が JSON として解釈できない場合、無ガードの json.loads が
    JSONDecodeError を漏らさず RuntimeError にラップされることを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": "no json here at all"}}]}
    ).encode()

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError, match="not valid JSON"):
            _call_openai("prompt", backend)


def test_call_claude_cli_non_json_stdout_raises_runtime_error() -> None:
    """
    claude --print が exit 0 で非JSON (バナー等) を出した場合、無ガードの
    json.loads が JSONDecodeError を漏らさず RuntimeError にラップされることを確認する
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Welcome to Claude Code\n(not json)"

    with patch("codeatrium.llm.subprocess.run", return_value=mock_result):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with pytest.raises(RuntimeError, match="non-JSON stdout"):
                _call_claude_cli("prompt")


def test_call_claude_cli_result_field_non_json_raises_runtime_error() -> None:
    """
    outer JSON は妥当だが result フィールドの中身が JSON でない場合も
    RuntimeError にラップされることを確認する
    """
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "not json inside result field"})

    with patch("codeatrium.llm.subprocess.run", return_value=mock_result):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with pytest.raises(RuntimeError, match="'result' field is not valid JSON"):
                _call_claude_cli("prompt")


def test_call_openai_validation_retry_changes_request_body() -> None:
    """
    validation 失敗後のリトライは、決定論的バックエンドが同じ失敗を繰り返さないよう
    是正メッセージを追記し temperature を変更した別の body を送ることを確認する
    (issue #15: 同一 body + temperature:0 の再送は無意味だった)
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"])

    call_count = {"count": 0}

    def validate_side_effect(palace: dict[str, Any]) -> dict[str, Any]:
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise LLMValidationError("room_assignments must be a list")
        return palace

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        with patch("codeatrium.llm._validate_palace", side_effect=validate_side_effect):
            _call_openai("prompt", backend)

    assert mock_urlopen.call_count == 2
    first_body = json.loads(mock_urlopen.call_args_list[0][0][0].data)
    second_body = json.loads(mock_urlopen.call_args_list[1][0][0].data)

    assert first_body["temperature"] == 0
    assert second_body["temperature"] != first_body["temperature"]
    assert second_body["messages"][0]["content"] != first_body["messages"][0]["content"]
    assert "room_assignments must be a list" in second_body["messages"][0]["content"]


def test_call_openai_retries_on_5xx_then_succeeds() -> None:
    """
    HTTP 5xx はネットワーク層で bounded retry され、後続の成功応答を返すことを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"])

    with patch(
        "urllib.request.urlopen",
        side_effect=[_http_error(503), mock_response],
    ) as mock_urlopen:
        with patch("codeatrium.llm.time.sleep"):
            result = _call_openai("prompt", backend)

    assert mock_urlopen.call_count == 2
    assert result == MOCK_JSON_RESPONSE["structured_output"]


def test_call_openai_retries_on_timeout_then_succeeds() -> None:
    """
    タイムアウトはネットワーク層で bounded retry され、後続の成功応答を返すことを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )
    mock_response = _mock_openai_response(MOCK_JSON_RESPONSE["structured_output"])

    with patch(
        "urllib.request.urlopen",
        side_effect=[TimeoutError("timed out"), mock_response],
    ) as mock_urlopen:
        with patch("codeatrium.llm.time.sleep"):
            result = _call_openai("prompt", backend)

    assert mock_urlopen.call_count == 2
    assert result == MOCK_JSON_RESPONSE["structured_output"]


def test_call_openai_does_not_retry_on_4xx() -> None:
    """
    4xx は恒久的失敗として扱い、リトライせず即 RuntimeError を送出することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )

    with patch("urllib.request.urlopen", side_effect=_http_error(400)) as mock_urlopen:
        with patch("codeatrium.llm.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError):
                _call_openai("prompt", backend)

    assert mock_urlopen.call_count == 1
    assert not mock_sleep.called


def test_call_openai_raises_after_network_retries_exhausted() -> None:
    """
    5xx が bounded retry の回数を使い切った場合、最終的に RuntimeError を送出することを確認する
    """
    backend = DistillBackend(
        provider="openai", model="m", base_url="http://localhost:11434/v1"
    )

    with patch("urllib.request.urlopen", side_effect=_http_error(503)) as mock_urlopen:
        with patch("codeatrium.llm.time.sleep"):
            with pytest.raises(RuntimeError):
                _call_openai("prompt", backend)

    assert mock_urlopen.call_count == _MAX_NETWORK_ATTEMPTS
