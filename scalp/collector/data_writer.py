"""CSV append. OCX 콜백에서 호출하지 않는다. 배치 상한, 무제한 재시도 없음. phase별 파일 + 크기 회전."""
from __future__ import annotations

import csv
import datetime
import os
from typing import Any, Dict, IO, List, Optional, Sequence, Tuple

from scalp.collector.clock import recv_ns
from scalp.collector.realtime_buffer import RealtimeBuffer
from scalp.collector.stats import CollectorStats

DEFAULT_BATCH = 200
MAX_BATCH = 500
DEFAULT_MAX_BYTES = 200 * 1024 * 1024


def _row(record: Any) -> Dict[str, Any]:
    if isinstance(record, dict):
        return {k: v for k, v in record.items() if not str(k).startswith("_")}
    if hasattr(record, "as_csv_row"):
        return record.as_csv_row()
    return {k: v for k, v in dict(getattr(record, "__dict__", {})).items() if not str(k).startswith("_")}


class CsvWriter:
    """단일 파일 모드. 기존 단위 테스트 호환."""

    def __init__(
        self,
        path: str,
        fieldnames: Sequence[str],
        *,
        batch_limit: int = DEFAULT_BATCH,
        stats: Optional[CollectorStats] = None,
    ) -> None:
        self.path = path
        self.fieldnames = list(fieldnames)
        self.batch_limit = max(1, min(int(batch_limit), MAX_BATCH))
        self.stats = stats if stats is not None else CollectorStats()
        self._header_written = os.path.isfile(path) and os.path.getsize(path) > 0
        self._fp: Optional[IO[str]] = None
        self.rows_written = 0
        self.closed = False

    def _ensure_open(self) -> IO[str]:
        if self._fp is None:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._fp = open(self.path, "a", encoding="utf-8", newline="")
            if not self._header_written:
                csv.DictWriter(self._fp, fieldnames=self.fieldnames, extrasaction="ignore").writeheader()
                self._fp.flush()
                self._header_written = True
        return self._fp

    def _release(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def flush_from(self, buffer: RealtimeBuffer) -> int:
        batch = buffer.pop_batch(self.batch_limit)
        if not batch:
            return 0
        return self._write_batch(batch)

    def flush_all(self, buffer: RealtimeBuffer) -> int:
        total = 0
        while len(buffer):
            remaining = len(buffer)
            n = self.flush_from(buffer)
            if n == 0:
                if len(buffer) >= remaining:
                    break
                continue
            total += n
        return total

    def _write_batch(self, batch: List[Any]) -> int:
        t0 = recv_ns()
        lag = 0
        for rec in batch:
            rns = rec.get("recv_ns") if isinstance(rec, dict) else getattr(rec, "recv_ns", None)
            if rns is not None:
                lag = max(lag, t0 - int(rns))
        ok = False
        try:
            fp = self._ensure_open()
            w = csv.DictWriter(fp, fieldnames=self.fieldnames, extrasaction="ignore")
            rows = [_row(r) for r in batch]
            w.writerows(rows)
            fp.flush()
            ok = True
        except Exception:
            self.stats.incr("write_fail")
            self.stats.incr("dropped_write", len(batch))
        finally:
            self._release()
        if not ok:
            return 0
        dt = recv_ns() - t0
        self.stats.note_flush_ns(dt)
        if lag > self.stats.max_save_lag_ns:
            self.stats.max_save_lag_ns = lag
        self.stats.incr("write_ok", len(batch))
        self.rows_written += len(batch)
        return len(batch)

    def close(self) -> None:
        self._release()
        self.closed = True


class PhaseRotatingWriter:
    """observe / after_unreg 별 파일, 200MB 회전, 덮어쓰기 금지."""

    def __init__(
        self,
        directory: str,
        kind: str,
        fieldnames: Sequence[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        date_str: Optional[str] = None,
        stats: Optional[CollectorStats] = None,
    ) -> None:
        self.directory = directory
        self.kind = kind
        self.fieldnames = list(fieldnames)
        self.max_bytes = max(1, int(max_bytes))
        self.date_str = date_str or datetime.datetime.now().strftime("%Y%m%d")
        self.stats = stats if stats is not None else CollectorStats()
        self._handles: Dict[Tuple[str, str, int], IO[str]] = {}
        self._part: Dict[Tuple[str, str], int] = {}
        self._bytes: Dict[Tuple[str, str, int], int] = {}
        self._rows: Dict[Tuple[str, str, int], int] = {}
        self._closed = False
        os.makedirs(self.directory, exist_ok=True)

    def _code_of(self, rec: Dict[str, Any]) -> str:
        return str(rec.get("code") or "unknown")

    def _phase_of(self, rec: Dict[str, Any]) -> str:
        p = str(rec.get("phase") or "observe")
        return p if p in ("observe", "after_unreg") else "observe"

    def _filename(self, code: str, phase: str, part: int) -> str:
        return os.path.join(
            self.directory,
            f"{self.kind}_{code}_{self.date_str}_{phase}_part{part:03d}.csv",
        )

    def _next_part(self, code: str, phase: str) -> int:
        key = (code, phase)
        part = self._part.get(key, 0)
        while True:
            part += 1
            path = self._filename(code, phase, part)
            if not os.path.isfile(path):
                self._part[key] = part
                return part
            if os.path.getsize(path) < self.max_bytes:
                self._part[key] = part
                return part

    def _open(self, code: str, phase: str, part: int) -> IO[str]:
        hk = (code, phase, part)
        if hk in self._handles:
            return self._handles[hk]
        path = self._filename(code, phase, part)
        existed = os.path.isfile(path) and os.path.getsize(path) > 0
        fp = open(path, "a", encoding="utf-8", newline="")
        if not existed:
            csv.DictWriter(fp, fieldnames=self.fieldnames, extrasaction="ignore").writeheader()
            fp.flush()
        self._handles[hk] = fp
        self._bytes[hk] = os.path.getsize(path)
        self._rows.setdefault(hk, 0)
        return fp

    def write_record(self, rec: Dict[str, Any]) -> bool:
        if self._closed:
            rec["_write_failed"] = True
            self.stats.incr("write_fail")
            self.stats.incr("dropped_write")
            return False
        t0 = recv_ns()
        code = self._code_of(rec)
        phase = self._phase_of(rec)
        key = (code, phase)
        if key not in self._part:
            self._next_part(code, phase)
        part = self._part[key]
        row = _row(rec)
        try:
            encoded = ",".join(str(row.get(f, "")) for f in self.fieldnames) + "\n"
            nbytes = len(encoded.encode("utf-8"))
            hk = (code, phase, part)
            if hk in self._bytes and self._rows.get(hk, 0) > 0 and self._bytes[hk] + nbytes > self.max_bytes:
                old = self._handles.pop(hk, None)
                if old is not None:
                    old.flush()
                    old.close()
                part = part + 1
                while os.path.isfile(self._filename(code, phase, part)):
                    part += 1
                self._part[key] = part
                hk = (code, phase, part)
            fp = self._open(code, phase, part)
            csv.DictWriter(fp, fieldnames=self.fieldnames, extrasaction="ignore").writerow(row)
            fp.flush()
            self._bytes[hk] = self._bytes.get(hk, 0) + nbytes
            self._rows[hk] = self._rows.get(hk, 0) + 1
        except Exception:
            rec["_write_failed"] = True
            self.stats.incr("write_fail")
            self.stats.incr("dropped_write")
            return False
        rec["_written"] = True
        dt = recv_ns() - t0
        self.stats.note_flush_ns(dt)
        rns = rec.get("recv_ns")
        if rns is not None:
            lag = t0 - int(rns)
            if lag > self.stats.max_save_lag_ns:
                self.stats.max_save_lag_ns = lag
        self.stats.incr("write_ok")
        return True

    def file_inventory(self) -> List[Dict[str, Any]]:
        out = []
        for (code, phase, part), rows in sorted(self._rows.items()):
            path = self._filename(code, phase, part)
            out.append({
                "path": path,
                "code": code,
                "phase": phase,
                "part": part,
                "rows": rows,
                "bytes": self._bytes.get((code, phase, part), 0),
            })
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fp in list(self._handles.values()):
            try:
                fp.flush()
                fp.close()
            except Exception:
                pass
        self._handles.clear()
        self.stats.append_files(self.file_inventory())
