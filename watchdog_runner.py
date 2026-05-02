import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


@dataclass
class RunResult:
    exit_code: int
    output_lines: List[str]


@dataclass
class TraceInfo:
    file_path: str
    line_no: int
    error_line: str


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def run_app_once(cwd: str, target_script: str, extra_args: Optional[List[str]] = None) -> RunResult:
    """Run the app once and capture combined stdout/stderr."""
    cmd = [sys.executable, target_script] + list(extra_args or [])
    _log(f"실행 시작: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: List[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        s = line.rstrip("\n")
        lines.append(s)
        print(s, flush=True)
    proc.wait()
    _log(f"실행 종료: exit_code={proc.returncode}")
    return RunResult(exit_code=int(proc.returncode or 0), output_lines=lines)


def parse_traceback(lines: List[str], cwd: str) -> Optional[TraceInfo]:
    """Parse the last Python traceback location and exception line."""
    if not lines:
        return None
    file_line_re = re.compile(r'File "(.+?)", line (\d+)')
    file_match: Optional[Tuple[str, int]] = None
    err_line = ""
    for ln in reversed(lines):
        if not err_line and re.search(r"(Error|Exception):", ln):
            err_line = ln.strip()
        m = file_line_re.search(ln)
        if m:
            p = m.group(1)
            n = int(m.group(2))
            if not os.path.isabs(p):
                p = os.path.abspath(os.path.join(cwd, p))
            file_match = (p, n)
            break
    if file_match is None:
        return None
    return TraceInfo(file_path=file_match[0], line_no=file_match[1], error_line=err_line)


def safe_read(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def safe_write(path: str, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def backup_file(path: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{path}.autofix.{ts}.bak"
    shutil.copy2(path, backup)
    return backup


def fix_dataframe_tolist(trace: TraceInfo) -> Optional[str]:
    """
    Handle:
    AttributeError: 'DataFrame' object has no attribute 'tolist'
    Replaces '.tolist()' with '.to_numpy().reshape(-1).tolist()' at error line.
    """
    if "DataFrame" not in trace.error_line or "tolist" not in trace.error_line:
        return None
    if not os.path.exists(trace.file_path):
        return None

    lines = safe_read(trace.file_path)
    idx = trace.line_no - 1
    if idx < 0 or idx >= len(lines):
        return None

    original = lines[idx]
    if ".tolist()" not in original:
        # fallback: search nearby lines
        start = max(0, idx - 3)
        end = min(len(lines), idx + 4)
        for i in range(start, end):
            if ".tolist()" in lines[i]:
                idx = i
                original = lines[i]
                break
        else:
            return None

    replaced = original.replace(".tolist()", ".to_numpy().reshape(-1).tolist()")
    if replaced == original:
        return None

    backup = backup_file(trace.file_path)
    lines[idx] = replaced
    safe_write(trace.file_path, lines)
    return (
        f"자동수정 적용: DataFrame tolist 오류 수정 "
        f"({os.path.basename(trace.file_path)}:{idx+1}) backup={os.path.basename(backup)}"
    )


def fix_dictionary_changed_size(trace: TraceInfo) -> Optional[str]:
    """
    Handle RuntimeError: dictionary changed size during iteration.
    Converts 'for k in d:' style to snapshot iteration for the failing line.
    """
    if "dictionary changed size during iteration" not in trace.error_line:
        return None
    if not os.path.exists(trace.file_path):
        return None
    lines = safe_read(trace.file_path)
    idx = trace.line_no - 1
    if idx < 0 or idx >= len(lines):
        return None
    original = lines[idx]
    # very conservative transform
    m = re.search(r"for\s+(.+?)\s+in\s+(.+?):\s*$", original)
    if not m:
        return None
    indent = original[: len(original) - len(original.lstrip(" "))]
    var = m.group(1)
    iterable = m.group(2)
    replaced = f"{indent}for {var} in list({iterable}):"
    backup = backup_file(trace.file_path)
    lines[idx] = replaced
    safe_write(trace.file_path, lines)
    return (
        f"자동수정 적용: dict iteration snapshot 처리 "
        f"({os.path.basename(trace.file_path)}:{idx+1}) backup={os.path.basename(backup)}"
    )


FIXERS: List[Callable[[TraceInfo], Optional[str]]] = [
    fix_dataframe_tolist,
    fix_dictionary_changed_size,
]


def try_autofix(trace: Optional[TraceInfo]) -> Optional[str]:
    if trace is None:
        return None
    for fixer in FIXERS:
        msg = fixer(trace)
        if msg:
            return msg
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="앱 실행 실패 시 자동수정 후 재실행 루프 (최대 5회)"
    )
    parser.add_argument("--script", default="main.py", help="실행할 파이썬 스크립트")
    parser.add_argument("--max-retries", type=int, default=5, help="최대 재시도 횟수")
    parser.add_argument(
        "--cooldown-sec", type=float, default=1.0, help="재실행 전 대기 시간(초)"
    )
    parser.add_argument(
        "--pw",
        default=os.environ.get("KIWOOM_ACCOUNT_PASSWORD", ""),
        help="main.py에 전달할 계좌 비밀번호",
    )
    args = parser.parse_args()

    cwd = os.getcwd()
    max_retries = max(1, int(args.max_retries))
    script = str(args.script)
    extra_args: List[str] = []
    if str(args.pw).strip():
        extra_args += ["--pw", str(args.pw).strip()]

    _log(
        f"자동복구 루프 시작: script={script}, max_retries={max_retries}, cwd={cwd}"
    )

    for attempt in range(1, max_retries + 1):
        _log(f"=== 시도 {attempt}/{max_retries} ===")
        result = run_app_once(cwd, script, extra_args=extra_args)
        if result.exit_code == 0:
            _log("정상 종료 감지. 루프 종료.")
            return 0

        trace = parse_traceback(result.output_lines, cwd)
        if trace is None:
            _log("원인 분석 실패: traceback 위치를 찾지 못함.")
            return result.exit_code or 1

        _log(
            f"원인 분석: file={trace.file_path}, line={trace.line_no}, error={trace.error_line}"
        )

        fix_msg = try_autofix(trace)
        if not fix_msg:
            _log("자동수정 가능한 규칙이 없어 루프 종료.")
            return result.exit_code or 1

        _log(fix_msg)
        _log(f"재실행 대기: {args.cooldown_sec:.1f}s")
        time.sleep(max(0.0, float(args.cooldown_sec)))

    _log(f"최대 반복 횟수({max_retries}) 도달. 종료.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

ㅐ