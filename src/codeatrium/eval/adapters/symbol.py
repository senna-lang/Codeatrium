"""Symbol adapter — the code→conversation lookup feature under test.

Wraps `context_lookup.resolve_u1`, the reverse-lookup `loci context
<file>:<symbol>` uses in production ("git blame for the conversation that
shaped this code"): `code_edges`/`code_symbols` populated at index time,
independent of distillation status. Only accepts `kind="symbol"` queries,
since it needs the file-grounded `"<file>::<symbol>"` payload that
`eval/gen/gen_symbol_recall.py` encodes.
"""

from __future__ import annotations

from pathlib import Path

from codeatrium.eval.datasets.schema import Query


class SymbolAdapter:
    id = "symbol"

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def retrieve(self, query: Query, k: int) -> list[str]:
        if query.kind != "symbol":
            return []
        file_path, sep, symbol_name = query.value.partition("::")
        if not sep or not symbol_name:
            return []

        from codeatrium.context_lookup import resolve_u1
        from codeatrium.db import get_connection

        con = get_connection(self._db_path)
        try:
            hits = resolve_u1(con, file_path, symbol_name, k)
        finally:
            con.close()
        return [hit.exchange_id for hit in hits]
