"""
Embedder のテスト
モデルロードを避けるため embed() は mock する
"""

from unittest.mock import MagicMock

from logo.embedder import Embedder


def test_embedder_returns_384_dim() -> None:
    """embed() が 384次元ベクトルを返す"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    vec = embedder.embed("テストクエリ")
    assert len(vec) == 384


def test_embedder_encode_called_with_text() -> None:
    """embed() がモデルの encode を呼ぶ"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    embedder.embed("テストクエリ")
    embedder._model.encode.assert_called_once()


def test_embedder_returns_float32() -> None:
    """embed() の結果が float32 の numpy 配列"""
    import numpy as np

    embedder = Embedder.__new__(Embedder)
    embedder._sock_path = None
    embedder._model = MagicMock()
    embedder._model.encode.return_value = np.zeros((1, 384), dtype="float32")

    vec = embedder.embed("テスト")
    assert vec.dtype == np.float32
