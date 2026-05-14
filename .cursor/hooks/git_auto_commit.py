# -*- coding: utf-8 -*-
"""
Cursor 훅: *.py 변경을 자동 커밋합니다.

1) 인자 없음 stdin JSON — afterFileEdit / afterTabFileEdit 편집 파일만 add·commit.
2) --session-end — stop 이벤트: 워킹트리에 남은 .py 변경(추적·미추적) 일괄 add·commit.

- git 미설치·저장소 아님·user.name 미설정이면 종료합니다(편집 차단 안 함).
- 대상 확장자는 *.py만 (data 로그 파일은 무시 패턴 또는 확장자로 제외).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlparse

CODE_SUFFIX = {".py", ".pyw"}


def _repo_root(start: Path) -> Path | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=90,
            shell=False,
        )
        if r.returncode != 0:
            return None
        p = Path(r.stdout.strip()).resolve()
        return p if p.is_dir() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _maybe_path_str(s: str, bucket: set[str]) -> None:
    raw = str(s).strip().strip('"').strip("'")
    if len(raw) < 5 or len(raw) > 600:
        return
    low = raw.lower()
    if not any(low.endswith(x) for x in CODE_SUFFIX):
        return
    if "\n" in raw or "\r" in raw or "```" in raw:
        return
    bucket.add(raw)


def _collect_paths(obj: object, bucket: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, str) and kl in {
                "path",
                "filepath",
                "file",
                "abspath",
                "target_file",
                "relativepath",
                "relative_workspace_path",
            }:
                _maybe_path_str(v, bucket)
            elif isinstance(v, str) and kl == "uri" and v.lower().startswith("file:"):
                tail = urlparse(v).path
                if tail:
                    _maybe_path_str(unquote(tail), bucket)
            else:
                _collect_paths(v, bucket)
    elif isinstance(obj, list):
        for x in obj:
            _collect_paths(x, bucket)
    elif isinstance(obj, str):
        _maybe_path_str(obj, bucket)


def _resolve_paths(repo: Path, raw: set[str]) -> list[Path]:
    ordered: list[Path] = []
    seen: set[str] = set()
    for s in sorted(raw):
        p = Path(s)
        if not p.is_absolute():
            p = (repo / p).resolve()
        else:
            p = p.resolve()
        try:
            p.relative_to(repo)
        except ValueError:
            continue
        if "__pycache__" in p.parts:
            continue
        if p.suffix.lower() not in CODE_SUFFIX:
            continue
        if not p.is_file():
            continue
        key = str(p)
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


def _normalize_git_path(line: str) -> str:
    s = str(line).strip().replace("\\", "/")
    if not s.endswith((".py", ".pyw")):
        return ""
    parts = Path(s).parts
    if "__pycache__" in parts:
        return ""
    return s


def _dirty_py_relative_paths(repo: Path) -> list[str]:
    out: set[str] = set()
    cmd_sets = (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    try:
        for cmd in cmd_sets:
            r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=90, shell=False)
            if r.returncode != 0:
                continue
            for raw in r.stdout.splitlines():
                rel = _normalize_git_path(raw)
                if not rel:
                    continue
                p_abs = (repo / rel.replace("/", os.sep)).resolve()
                try:
                    p_abs.relative_to(repo)
                except ValueError:
                    continue
                if not p_abs.is_file():
                    continue
                out.add(rel)
    except OSError:
        return []
    return sorted(out)


def _stage_paths(root: Path, rels: list[str]) -> bool:
    try:
        r_add = subprocess.run(
            ["git", "add", "--"] + rels,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        return r_add.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _cached_nonempty(root: Path) -> bool:
    try:
        r_empty = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(root),
            timeout=60,
            shell=False,
        )
        return r_empty.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_commit_paths(root: Path, rels: list[str]) -> None:
    if not rels or not _stage_paths(root, rels):
        return
    if not _cached_nonempty(root):
        return
    msg = "auto: cursor 저장 " + ", ".join(sorted(rels)[:10])
    if len(rels) > 10:
        msg += f" (+{len(rels) - 10})"
    try:
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _git_commit_flush_session(root: Path) -> None:
    dirty = _dirty_py_relative_paths(root)
    if not dirty:
        return
    norm = sorted({x.replace("\\", "/") for x in dirty})
    if not _stage_paths(root, norm):
        return
    if not _cached_nonempty(root):
        return
    msg = f"auto: cursor 세션 마무리 {len(norm)} py"
    if len(norm) <= 14:
        msg += " [" + ",".join(norm) + "]"
    try:
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def main() -> int:
    start = Path.cwd().resolve()
    root = _repo_root(start)
    if root is None:
        return 0

    if len(sys.argv) > 1 and sys.argv[1] == "--session-end":
        _git_commit_flush_session(root)
        return 0

    try:
        stdin = sys.stdin.read()
        if not stdin.strip():
            return 0
        data = json.loads(stdin)
    except json.JSONDecodeError:
        return 0

    bucket: set[str] = set()
    _collect_paths(data, bucket)
    paths = _resolve_paths(root, bucket)
    if not paths:
        return 0
    rels = [str(p.relative_to(root)).replace("\\", "/") for p in paths]
    _git_commit_paths(root, rels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
