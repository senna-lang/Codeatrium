"""call_claude のユニットテスト: subprocess.run をモックして振る舞いを検証する"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeatrium.llm import call_claude

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
            with patch(
                "codeatrium.llm._session_dir", return_value=tmp_path
            ):
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
            with patch(
                "codeatrium.llm._session_dir", return_value=tmp_path
            ):
                # TimeoutExpired が発生することを確認
                with pytest.raises(subprocess.TimeoutExpired):
                    call_claude("test prompt")

                # タイムアウト時にも副作用 .jsonl がクリーンアップされたことを確認
                assert not side_jsonl.exists()
