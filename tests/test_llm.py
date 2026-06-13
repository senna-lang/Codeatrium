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
    DistillBackend,
    LLMValidationError,
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
    --no-session-persistence, --setting-sources, --output-format (json),
    --model が含まれることを assert する
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
    _session_dir を tmp_path に向け、副作用 .jsonl が
    正常終了時にクリーンアップされることを確認する
    """
    side_jsonl = tmp_path / "side.jsonl"

    def fake_run(*args, **kwargs):
        # subprocess.run 呼び出し時（before スナップショット取得後）に副作用ファイルを作成
        side_jsonl.write_text('{"key": "value"}\n')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(MOCK_JSON_RESPONSE)
        return mock_result

    with patch("codeatrium.llm.subprocess.run", side_effect=fake_run):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("codeatrium.llm._session_dir", return_value=tmp_path):
                call_claude("test prompt")

                # 正常終了後は副作用 .jsonl がクリーンアップされたことを確認
                assert not side_jsonl.exists()


def test_call_claude_cleanup_on_timeout(tmp_path: Path) -> None:
    """
    subprocess.run が subprocess.TimeoutExpired を投げるようモックし:
    (1) call_claude が例外を送出する (pytest.raises)
    (2) それでも副作用 .jsonl のクリーンアップが走る (finally 経路) ことを確認する
    """
    side_jsonl = tmp_path / "side.jsonl"

    def fake_run(*args, **kwargs):
        # subprocess.run 呼び出し時（before スナップショット取得後）に副作用ファイルを作成
        side_jsonl.write_text('{"key": "value"}\n')
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

                # タイムアウト時にも副作用 .jsonl がクリーンアップされたことを確認
                assert not side_jsonl.exists()


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
