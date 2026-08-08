"""蒸留 ModelClient の discover/setup/resolve。

v1 必須 client: ollama-ft, claude-cli。openai-compat は config 明示時のみ resolve
対象になる（自動検出しない — 汎用エンドポイントを推測すると誤検出のリスクが高い）。

silent fallback 禁止: discover() は「今 Ready なもの」だけを返す。呼び出し側
（cli/init, distill --setup, runtime reselect）が Ready 一覧から選ばせる。
"""

from __future__ import annotations

import shutil
import subprocess

from codeatrium.adapters.model.types import ClientStatus, ModelClient
from codeatrium.config import LOCAL_DISTILL_BASE_URL, LOCAL_DISTILL_MODEL

# v1 で discover() が調べる client id（表示順 = recommended 優先度）
DISCOVERABLE_CLIENT_IDS = ("ollama-ft", "claude-cli")


def _ollama_model_pulled(model: str) -> bool:
    """`ollama list` の出力に model 名（タグ部分を除いた repo 名）が含まれるか確認する。"""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if result.returncode != 0:
        return False
    # `ollama list` は "NAME" 列にタグ付きモデル名を表示する。完全一致 or 前方一致で判定。
    lines = result.stdout.splitlines()
    return any(model in line for line in lines)


def detect_ollama_ft() -> ClientStatus:
    """Ollama binary + FT model の pull 状態を確認する"""
    if shutil.which("ollama") is None:
        return ClientStatus(
            id="ollama-ft",
            label="Ollama (local FT model)",
            state="unavailable",
            reason="ollama binary not found in PATH",
        )
    if not _ollama_model_pulled(LOCAL_DISTILL_MODEL):
        return ClientStatus(
            id="ollama-ft",
            label="Ollama (local FT model)",
            state="setupable",
            reason=f"model not pulled: {LOCAL_DISTILL_MODEL}",
        )
    return ClientStatus(
        id="ollama-ft",
        label="Ollama (local FT model)",
        state="ready",
        reason="ready",
        client=ModelClient(
            id="ollama-ft",
            provider="openai",
            model=LOCAL_DISTILL_MODEL,
            base_url=LOCAL_DISTILL_BASE_URL,
            label="Ollama (local FT model)",
        ),
    )


def detect_claude_cli() -> ClientStatus:
    """claude CLI の PATH 有無のみ確認する（login probe はしない — D7）"""
    from codeatrium.config import DEFAULT_DISTILL_MODEL

    if shutil.which("claude") is None:
        return ClientStatus(
            id="claude-cli",
            label="Claude CLI",
            state="unavailable",
            reason="claude CLI not found in PATH",
        )
    return ClientStatus(
        id="claude-cli",
        label="Claude CLI",
        state="ready",
        reason="ready",
        client=ModelClient(
            id="claude-cli",
            provider="claude",
            model=DEFAULT_DISTILL_MODEL,
            base_url=None,
            label="Claude CLI",
        ),
    )


_DETECTORS = {
    "ollama-ft": detect_ollama_ft,
    "claude-cli": detect_claude_cli,
}


def discover() -> list[ClientStatus]:
    """v1 必須 client を検出順（ollama-ft, claude-cli）で返す"""
    return [_DETECTORS[client_id]() for client_id in DISCOVERABLE_CLIENT_IDS]


def ready_clients(statuses: list[ClientStatus]) -> list[ClientStatus]:
    return [s for s in statuses if s.state == "ready"]


def recommended_id(statuses: list[ClientStatus]) -> str | None:
    """Ready なら ollama-ft を推奨、なければ最初の Ready、なければ None"""
    ready = ready_clients(statuses)
    if not ready:
        return None
    for s in ready:
        if s.id == "ollama-ft":
            return s.id
    return ready[0].id


def setup(client_id: str) -> tuple[bool, str]:
    """setupable な client を Ready にする（今は ollama-ft の `ollama pull` のみ）。

    binary 自体のインストールは実行しない — 案内のみ（D6）。
    """
    if client_id != "ollama-ft":
        return False, f"no automated setup for {client_id}"
    if shutil.which("ollama") is None:
        return False, (
            "ollama binary not found — install from "
            "https://ollama.com then retry"
        )
    try:
        result = subprocess.run(
            ["ollama", "pull", LOCAL_DISTILL_MODEL],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"ollama pull failed: {e}"
    if result.returncode != 0:
        return False, f"ollama pull failed: {result.stderr.strip()}"
    return True, f"pulled {LOCAL_DISTILL_MODEL}"


def resolve_client(client_id: str, cfg) -> ModelClient:
    """config の distill_client id から ModelClient を組み立てる
    （detect はしない — 呼び出し側の責務）"""
    from codeatrium.config import DEFAULT_DISTILL_MODEL

    if client_id == "claude-cli":
        return ModelClient(
            id="claude-cli",
            provider="claude",
            model=cfg.distill_model or DEFAULT_DISTILL_MODEL,
            base_url=None,
            label="Claude CLI",
        )
    if client_id == "ollama-ft":
        return ModelClient(
            id="ollama-ft",
            provider="openai",
            model=cfg.distill_model or LOCAL_DISTILL_MODEL,
            base_url=cfg.distill_base_url or LOCAL_DISTILL_BASE_URL,
            label="Ollama (local FT model)",
        )
    if client_id == "openai-compat":
        if not cfg.distill_base_url:
            raise ValueError("openai-compat client requires distill.base_url")
        return ModelClient(
            id="openai-compat",
            provider="openai",
            model=cfg.distill_model,
            base_url=cfg.distill_base_url,
            label="OpenAI-compatible endpoint",
        )
    raise ValueError(f"unknown distill client id: {client_id}")


def check_ready(client_id: str) -> ClientStatus:
    """単一 client の現在の Ready 状態を再確認する（runtime reselect 用）"""
    detector = _DETECTORS.get(client_id)
    if detector is None:
        # openai-compat は自動検出対象外。config の base_url が設定されていれば
        # Ready 扱い（実際の疎通は distill 実行時に確認される）。
        return ClientStatus(
            id=client_id,
            label=client_id,
            state="unavailable",
            reason=f"no detector for {client_id}",
        )
    return detector()


def write_client_config(config_path, client: ModelClient) -> None:
    """config.toml の [distill] を client/model/base_url で上書きする（他セクションは保持、
    legacy `provider` キーは書かない）。init と `loci distill --setup` の共通実装。
    """
    import tomllib

    existing: dict = {}
    if config_path.exists():
        with config_path.open("rb") as f:
            existing = tomllib.load(f)

    distill = dict(existing.get("distill", {}))
    distill.pop("provider", None)
    distill["client"] = client.id
    distill["model"] = client.model
    if client.base_url:
        distill["base_url"] = client.base_url
    else:
        distill.pop("base_url", None)

    lines = ["# Codeatrium configuration", "", "[distill]"]
    for key in ("client", "model", "base_url", "batch_limit", "min_chars"):
        if key not in distill:
            continue
        val = distill[key]
        line = f'{key} = "{val}"' if isinstance(val, str) else f"{key} = {val}"
        lines.append(line)
    lines.append("")
    lines.append("[index]")
    index_min_chars = existing.get("index", {}).get("min_chars")
    if index_min_chars is not None:
        lines.append(f"min_chars = {index_min_chars}")
    else:
        lines.append("# min_chars = 50   # trivial フィルタ閾値（文字数）")
    lines.append("")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines))
