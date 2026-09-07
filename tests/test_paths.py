"""find_project_root の探索動作と親ディレクトリ通知のテスト"""

from __future__ import annotations

from pathlib import Path

from codeatrium.paths import find_project_root


def _mock_git_root(monkeypatch, root):
    """git_root() を固定値返却にモックする（テスト中は実 git を呼ばない）"""
    monkeypatch.setattr("codeatrium.paths.git_root", lambda: root)


def test_find_project_root_uses_cwd_when_initialized(tmp_path, monkeypatch, capsys):
    """cwd 直下に .codeatrium/ がある場合は cwd を返し、通知を出さない"""
    (tmp_path / ".codeatrium").mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert "parent directory" not in captured.err


def test_find_project_root_walks_to_parent_with_notice(tmp_path, monkeypatch, capsys):
    """サブディレクトリで実行時、親の .codeatrium/ を拾った場合は stderr に通知"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert "parent directory" in captured.err
    assert str(tmp_path) in captured.err


def test_find_project_root_notify_false_suppresses_notice(
    tmp_path, monkeypatch, capsys
):
    """notify=False で親通知を抑止できる"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root(notify=False)
    captured = capsys.readouterr()

    assert root == tmp_path
    assert captured.err == ""


def test_find_project_root_uninitialized_returns_git_root_silently(
    tmp_path, monkeypatch, capsys
):
    """git 内で .codeatrium/ が見つからない場合は git root を返し、通知も出さない

    （呼び出し側が db.exists() で "Not initialized" を出す責務を持つ）
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    _mock_git_root(monkeypatch, tmp_path)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == tmp_path
    assert captured.err == ""


def test_find_project_root_does_not_cross_git_root(tmp_path, monkeypatch, capsys):
    """別プロジェクト（git root 外）の .codeatrium/ は拾わない"""
    # 親（git 管理外）に .codeatrium/ を配置
    (tmp_path / ".codeatrium").mkdir()

    # 子に独立した git リポジトリ
    inner = tmp_path / "inner_repo"
    inner.mkdir()
    _mock_git_root(monkeypatch, inner)
    monkeypatch.chdir(inner)

    root = find_project_root()
    captured = capsys.readouterr()

    # inner の git root が返り、外の .codeatrium/ は無視される
    assert root == inner
    assert "parent directory" not in captured.err


def test_find_project_root_non_git_does_not_walk_parent(
    tmp_path, monkeypatch, capsys
):
    """git 外のサブディレクトリでは親探索しない"""
    (tmp_path / ".codeatrium").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()

    _mock_git_root(monkeypatch, None)
    monkeypatch.chdir(sub)

    root = find_project_root()
    captured = capsys.readouterr()

    # cwd を返す（親の .codeatrium/ は拾わない）
    assert root == sub
    assert captured.err == ""


def test_resolve_grok_sessions_path_percent_encodes_project_root(
    tmp_path: Path, monkeypatch
) -> None:
    """grok は cwd の絶対パスを percent-encode（`/` も `%2F`）したディレクトリ名を使う。"""
    from urllib.parse import quote

    from codeatrium.paths import resolve_grok_sessions_path

    home = tmp_path / "home"
    project_root = tmp_path / "work" / "myrepo"
    session_dir = home / ".grok" / "sessions" / quote(str(project_root), safe="")
    (session_dir / "019f-abcd").mkdir(parents=True)
    (session_dir / "019f-abcd" / "updates.jsonl").write_text("{}\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert resolve_grok_sessions_path(project_root) == session_dir


def test_resolve_grok_sessions_path_returns_none_for_unknown_project(
    tmp_path: Path, monkeypatch
) -> None:
    """セッションが無いプロジェクトでは None を返す（誤って別プロジェクトを拾わない）。"""
    from codeatrium.paths import resolve_grok_sessions_path

    home = tmp_path / "home"
    (home / ".grok" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert resolve_grok_sessions_path(tmp_path / "work" / "other") is None


def test_find_project_root_resolves_symlinked_cwd_before_comparing_to_git_root(
    tmp_path, monkeypatch, capsys
):
    """symlink 配下の cwd でも resolve() してから git_root() と比較するため、
    break 条件 `p == root` が成立し、リポジトリ外の `.codeatrium/` を拾わない。

    実 OS の os.getcwd() は通常シンボリックリンクを解決済みで返すため、ここでは
    バグを意図的に再現するために Path.cwd() を未解決のシンボリックリンクパスに
    差し替える（issue #27）。
    """
    # 別プロジェクト（git リポジトリ外）の .codeatrium/
    (tmp_path / ".codeatrium").mkdir()

    real_repo = tmp_path / "real_repo"
    real_repo.mkdir()
    symlinked_cwd = tmp_path / "link_repo"
    symlinked_cwd.symlink_to(real_repo)

    _mock_git_root(monkeypatch, real_repo)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: symlinked_cwd))

    root = find_project_root()
    captured = capsys.readouterr()

    assert root == real_repo
    assert "parent directory" not in captured.err


def test_resolve_claude_projects_path_folds_non_alnum_like_claude_does(
    tmp_path: Path, monkeypatch
) -> None:
    """Claude Code は "/" だけでなく "." など英数字以外の文字全般を "-" へ畳んだ
    ディレクトリ名を使う。project_root に "." を含む場合でも実セッション
    ディレクトリを見つけられる（issue #27）。

    期待値は実装と同じ非英数字置換ルールで組み立てる。tmp_path の絶対パス自体に
    "_" などの記号が含まれる環境依存の値であっても、テストが環境非依存に成立する
    ようにするため（"/" のみの単純な置換では tmp_path 由来の "_" を考慮できず、
    このテスト環境では一致しない）。
    """
    import re

    from codeatrium.paths import resolve_claude_projects_path

    claude_projects = tmp_path / "claude_projects"
    project_root = tmp_path / "work" / "my.repo.v1"
    project_root.mkdir(parents=True)

    encoded_name = re.sub(r"[^a-zA-Z0-9]", "-", str(project_root))
    session_dir = claude_projects / encoded_name
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("{}\n")

    monkeypatch.setattr("codeatrium.paths.CLAUDE_PROJECTS_DIR", claude_projects)

    assert resolve_claude_projects_path(project_root) == session_dir

    # "/" のみを置換する旧実装ではヒットしないことを明示（回帰確認）
    naive_encoded_name = str(project_root).replace("/", "-")
    assert naive_encoded_name != encoded_name


def test_loci_bin_prefers_venv_binary_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    """venv 配下に loci が存在する場合はそれを優先する（PATH 非依存の従来動作を維持）。"""
    from codeatrium.paths import loci_bin

    fake_python = tmp_path / "venv" / "bin" / "python3"
    fake_python.parent.mkdir(parents=True)
    venv_loci = fake_python.parent / "loci"
    venv_loci.write_text("#!/bin/sh\n")

    monkeypatch.setattr("codeatrium.paths.sys.executable", str(fake_python))
    monkeypatch.setattr(
        "codeatrium.paths.shutil.which", lambda name: "/should/not/be/used"
    )

    assert loci_bin() == str(venv_loci)


def test_loci_bin_falls_back_to_which_under_pipx_install(
    tmp_path: Path, monkeypatch
) -> None:
    """venv 配下に loci が無い場合（pipx/global インストール）、
    shutil.which("loci") にフォールバックする（issue #27）。
    """
    from codeatrium.paths import loci_bin

    fake_python = tmp_path / "some_venv" / "bin" / "python3"
    fake_python.parent.mkdir(parents=True)
    global_loci = tmp_path / "usr_local_bin" / "loci"
    global_loci.parent.mkdir(parents=True)
    global_loci.write_text("#!/bin/sh\n")

    monkeypatch.setattr("codeatrium.paths.sys.executable", str(fake_python))
    monkeypatch.setattr(
        "codeatrium.paths.shutil.which",
        lambda name: str(global_loci) if name == "loci" else None,
    )

    assert loci_bin() == str(global_loci)


def test_loci_bin_warns_when_unresolved(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """venv にも PATH にも loci が見つからない場合、stderr に警告しつつ
    従来の venv パスをフォールバックとして返す（issue #27）。
    """
    from codeatrium.paths import loci_bin

    fake_python = tmp_path / "some_venv" / "bin" / "python3"
    fake_python.parent.mkdir(parents=True)

    monkeypatch.setattr("codeatrium.paths.sys.executable", str(fake_python))
    monkeypatch.setattr("codeatrium.paths.shutil.which", lambda name: None)

    result = loci_bin()
    captured = capsys.readouterr()

    assert result == str(fake_python.parent / "loci")
    assert "Warning" in captured.err
