# Changelog

## [0.2.0] - 2026-04-21

### Added

- `loci init --no-hooks` flag to skip automatic Claude Code hook registration.
- `EmbedderSetupError` exception for environment-level embedder failures (distinguishes them from per-row errors).
- SVG banner at the top of the README (`assets/banner.svg`, generated via Freeze).
- `scripts/generate-banner.sh` to regenerate the banner from the live CLI output.

### Changed

- `loci init` now registers Claude Code hooks automatically at the end of setup — previously required a separate `loci hook install` step. Use `--no-hooks` to opt out.
- Interactive prompts re-prompt on invalid input instead of silently falling back to a default. The "run distillation now?" prompt now accepts `y`/`n`/`yes`/`no` in addition to `1`/`2`.
- Custom exchange counts are range-validated (`1..total`) and custom `min_chars` requires `>= 0`.
- Startup banner uses the pagga half-block font with a blue vertical gradient.

### Fixed

- `loci init` cleans up `.codeatrium/` automatically if the execution phase fails or is interrupted (`KeyboardInterrupt`), so re-running is safe.
- A single corrupt `.jsonl` no longer aborts the whole indexing loop — it logs a warning and continues.
- `git_root()` catches `FileNotFoundError` when the `git` binary is missing.
- `parse_exchanges` returns `[]` for missing files instead of raising.
- Distillation failures from `sentence_transformers` import issues (e.g. numpy/pyarrow binary mismatch) now print a single friendly message with remediation hints instead of a full traceback followed by per-row error spam.

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
