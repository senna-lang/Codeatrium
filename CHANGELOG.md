# Changelog

## [0.1.0] - 2026-03-31

### Added

- `loci init` — initialize `.codeatrium/` in project root
- `loci index` — parse `.jsonl` session logs, split into exchanges, embed with multilingual-e5-small
- `loci distill` — distill exchanges via `claude --print` into palace objects (exchange_core, specific_context, room_assignments)
- `loci search` — cross-layer RRF fusion search (BM25 verbatim + HNSW distilled)
- `loci context` — reverse lookup: code symbol → past conversations
- `loci show` — fetch verbatim exchange by ref
- `loci status` — show index state
- `loci server start/stop/status` — Unix socket embedding server for <0.2s search
- `loci hook install` — register Claude Code SessionStart/Stop hooks
- `config.toml` support for distill model and batch limit
- tree-sitter symbol resolution (Python, TypeScript, Go)
- Bilingual support (Japanese + English) via multilingual-e5-small
