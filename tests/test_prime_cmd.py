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
    """PRIME_TEXT must contain a concrete U1 (file+symbol) loci context example
    (design §6.1: --symbol is no longer the primary form shown to agents)"""
    assert "loci context src/codeatrium/search.py:search_combined" in PRIME_TEXT


def test_prime_text_context_section_is_marked_primary_and_comes_first():
    """design §6.4: loci context must be introduced as the primary way to use the tool,
    and its section must appear before the loci search section"""
    assert "primary" in PRIME_TEXT.lower()
    context_idx = PRIME_TEXT.index("### Context")
    search_idx = PRIME_TEXT.index("### Search")
    assert context_idx < search_idx


def test_prime_text_search_section_is_marked_secondary():
    """design §6.4: loci search must be demoted to a secondary fallback"""
    search_heading_end = PRIME_TEXT.index("\n", PRIME_TEXT.index("### Search"))
    assert "secondary" in PRIME_TEXT[PRIME_TEXT.index("### Search") : search_heading_end].lower()


def test_prime_text_context_section_explains_bidirectional_recall():
    """PRIME_TEXT context section must convey the symbol-to-memory design intent"""
    text_lower = PRIME_TEXT.lower()
    assert any(
        phrase in text_lower for phrase in ["recall", "reverse lookup", "memory about"]
    )


# ---- IDE selection trigger contract ----


def test_prime_text_has_ide_selection_section():
    """PRIME_TEXT must explain IDE selection as a deictic anchor for context recall"""
    assert "IDE selection as a deictic anchor" in PRIME_TEXT


def test_prime_text_ide_selection_requires_conjunction():
    """A selection alone must NOT trigger recall — requires selection AND a recall need"""
    text_lower = PRIME_TEXT.lower()
    assert "only when both" in text_lower
    # both branches of the recall need must be present
    assert "asks about the past" in text_lower
    assert "your own next action" in text_lower


def test_prime_text_ide_selection_guards_against_over_fire():
    """PRIME_TEXT must tell the agent NOT to recall on edit-only selections"""
    assert "Do NOT recall" in PRIME_TEXT


def test_prime_text_ide_selection_no_longer_requires_manual_symbol_resolution():
    """design §6.4: the agent must NOT be asked to resolve the enclosing symbol itself
    (e.g. via LSP) before calling loci context — codeatrium resolves <file>:<line> for it.
    This intentionally supersedes the old instruction to look up the enclosing symbol
    via LSP before calling loci context --symbol."""
    assert "LSP" not in PRIME_TEXT
    assert "resolves it for you" in PRIME_TEXT
    assert "loci context <file>:<line>" in PRIME_TEXT


def test_prime_text_ide_selection_has_search_fallback():
    """When no single symbol applies or context is empty, fall back to loci search"""
    text_lower = PRIME_TEXT.lower()
    assert "spans multiple symbols" in text_lower
    assert "return no results" in text_lower


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
