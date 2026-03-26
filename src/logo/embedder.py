"""
sentence-transformers / multilingual-e5-small の embedding ラッパー

モデル: intfloat/multilingual-e5-small（384次元・日本語+英語混在対応・CPU動作）
cold start が問題になった場合は SentenceTransformer(..., backend="onnx") で対処する
"""

from __future__ import annotations

import numpy as np

MODEL_NAME = "intfloat/multilingual-e5-small"


class Embedder:
    """multilingual-e5-small の薄いラッパー。遅延ロード。"""

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(MODEL_NAME)

    def embed(self, text: str) -> np.ndarray:
        """テキストを 384次元 float32 ベクトルに変換する"""
        # multilingual-e5 は "query: " / "passage: " プレフィックスを推奨
        result = self._model.encode(
            [f"query: {text}"],
            normalize_embeddings=True,
        )
        return result[0].astype(np.float32)

    def embed_passage(self, text: str) -> np.ndarray:
        """インデックス登録用（passage プレフィックス）"""
        result = self._model.encode(
            [f"passage: {text}"],
            normalize_embeddings=True,
        )
        return result[0].astype(np.float32)
