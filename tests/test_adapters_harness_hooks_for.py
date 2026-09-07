"""hooks_for() のルーティングと FallbackHooks の非目標維持を検証する（issue #40）。

omp-pi/opencode/grok がネイティブ実装へ切り替わった後も、FallbackHooks
クラス自体は「未知の6つ目以降の harness のための安全網」として変更しない
（issue #40 の非目標）。`hooks_for` からは現在どの既知 harness からも
到達しないが、クラスは直接インスタンス化できる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeatrium.adapters.harness.hooks import (
    ClaudeHooks,
    CodexHooks,
    FallbackHooks,
    GrokHooks,
    OmpPiHooks,
    OpenCodeHooks,
    hooks_for,
)


@pytest.mark.parametrize(
    ("harness", "expected_type"),
    [
        ("claude", ClaudeHooks),
        ("codex", CodexHooks),
        ("omp-pi", OmpPiHooks),
        ("opencode", OpenCodeHooks),
        ("grok", GrokHooks),
    ],
)
def test_hooks_for_routes_every_known_harness_to_native_implementation(
    harness: str, expected_type: type
) -> None:
    assert isinstance(hooks_for(harness), expected_type)


def test_hooks_for_raises_for_unknown_harness() -> None:
    with pytest.raises(ValueError, match="Unsupported harness"):
        hooks_for("some-future-harness")


def test_fallback_hooks_install_always_returns_false() -> None:
    """FallbackHooks 自体の挙動は変更しない（issue #40 非目標）。"""
    fallback = FallbackHooks("some-future-harness")

    changed, message = fallback.install(Path("/tmp/project"))

    assert changed is False
    assert "some-future-harness" in message
    assert "loci index --harness some-future-harness" in message


def test_fallback_hooks_uninstall_always_returns_false() -> None:
    fallback = FallbackHooks("some-future-harness")

    changed, message = fallback.uninstall()

    assert changed is False
    assert "no native codeatrium hooks" in message
