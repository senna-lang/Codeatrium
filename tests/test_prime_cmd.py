"""tests for prime_cmd — PRIME_TEXT contract + inject_claude_md idempotency"""

from __future__ import annotations

from pathlib import Path

from codeatrium.cli.prime_cmd import (
    BEGIN_MARKER,
    END_MARKER,
    PRIME_TEXT,
    inject_claude_md,
)

# ---- PRIME_TEXT contract ----


def test_prime_text_has_loci_context_section_heading():
    """PRIME_TEXT must contain an independent section heading for loci context"""
    assert "### Context" in PRIME_TEXT


def test_prime_text_has_agent_action_triggers():
    """PRIME_TEXT must list agent-initiated action triggers for edit/refactor, new impl, and error"""
    assert "Before editing or refactoring" in PRIME_TEXT
    assert "Before starting a new implementation" in PRIME_TEXT
    assert "encounter" in PRIME_TEXT


def test_prime_text_has_concrete_search_example():
    """PRIME_TEXT must contain a concrete loci search example (not a bare placeholder)"""
    assert 'loci search "BM25 RRF fusion ranking"' in PRIME_TEXT


def test_prime_text_has_concrete_context_example():
    """PRIME_TEXT must contain a concrete loci context example with a real symbol"""
    assert 'loci context --symbol "SymbolResolver.extract"' in PRIME_TEXT


def test_prime_text_context_section_explains_bidirectional_recall():
    """PRIME_TEXT context section must convey the symbol-to-memory design intent"""
    text_lower = PRIME_TEXT.lower()
    assert any(
        phrase in text_lower for phrase in ["recall", "reverse lookup", "memory about"]
    )


# ---- inject_claude_md idempotency ----


def test_inject_claude_md_creates_file_when_absent(tmp_path: Path):
    """inject_claude_md creates CLAUDE.md when it does not exist"""
    result = inject_claude_md(tmp_path)
    assert result is True
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists()
    content = claude_md.read_text()
    assert BEGIN_MARKER in content
    assert END_MARKER in content


def test_inject_claude_md_idempotent_on_second_call(tmp_path: Path):
    """inject_claude_md returns False on second call when content is already up-to-date"""
    inject_claude_md(tmp_path)
    result2 = inject_claude_md(tmp_path)
    assert result2 is False


def test_inject_claude_md_second_call_does_not_modify_content(tmp_path: Path):
    """inject_claude_md leaves file content unchanged on second call"""
    inject_claude_md(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    content_after_first = claude_md.read_text()
    inject_claude_md(tmp_path)
    content_after_second = claude_md.read_text()
    assert content_after_first == content_after_second
