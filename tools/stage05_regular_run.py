"""정규장 전용 0.5단계 실행기.

이번 단계 목표: 수익이 아니라, 키움 실시간 데이터가 들어오고 주문 안전장치가 막혀 있는지 확인.

강제 조건
  * 평일 09:00~15:30 에만 실행. 장외면 키움에 접속하지 않고 종료한다.
  * --force-offhours 금지
  * disconnect 단계 금지 (별도 승인 전까지)
  * LIVE_ORDERS_ENABLED=0 강제
  * SendOrder / 매수·매도 / 취소 주문은 하위 스크립트 가드가 차단한다.
  * main.py 등 제품 코드는 수정하지 않는다.

사용:
  python -X utf8 tools\\stage05_regular_run.py
      → 정규장(09:00~15:30)이면 즉시 검증
      → 평일 07:00~09:00 이면 지금 모의투자 로그인 후 09:00까지 대기했다가 검증
  python -X utf8 tools\\stage05_regular_run.py --login-now-wait
      → 장 시작 전(07:00 이전도 가능)에 모의투자 로그인만 하고, 09:00에 실시간 검증을 시작한다
  python -X utf8 tools\\stage05_regular_run.py --login-check
      → 모의투자 로그인만 확인하고 종료 (장외 가능, 실시간 검증 없음)
결과:  log/stage0_verify/stage05_verify_날짜시간.txt/.json
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY = os.path.join(ROOT, "tools", "stage05_kiwoom_verify.py")
RC_OFFHOURS = 20
RC_FORBIDDEN = 21
RC_BUSY = 22
WAIT_FLAG = "--login-now-wait"
CHECK_FLAG = "--login-check"


def is_regular_session(now: Optional[datetime.datetime] = None) -> bool:
    """평일 09:00~15:30. 휴장일은 알 수 없으므로 요일·시각만 본다."""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm < 15 * 60 + 30


def preflight(argv: Sequence[str], now: Optional[datetime.datetime] = None) -> Tuple[bool, str, int]:
    """실행 가능하면 (True, '', 0). 아니면 (False, 사유, 종료코드). 키움에 접속하지 않는다."""
    joined = " ".join(str(a) for a in argv)
    low = joined.lower()
    if "--force-offhours" in low or "force_offhours" in low.replace("-", "_"):
        return False, "금지: --force-offhours 는 이번 정규장 검증에서 사용할 수 없다", RC_FORBIDDEN
    if "disconnect" in low:
        return False, "금지: 연결 단절 테스트는 별도 승인 전까지 실행하지 않는다", RC_FORBIDDEN
    if "--phases" in low:
        return False, "금지: --phases 변경 불가. 이 실행기는 realtime,orders 만 돌린다", RC_FORBIDDEN
    now = now or datetime.datetime.now()
    wait = WAIT_FLAG in joined
    check = CHECK_FLAG in joined
    if wait and check:
        return False, "금지: --login-now-wait 와 --login-check 는 함께 쓰지 않는다", RC_FORBIDDEN
    if check:
        return True, "login-check", 0
    if wait:
        if now.weekday() >= 5:
            return False, "주말 — 정규장 대기를 시작하지 않는다", RC_OFFHOURS
        if (now.hour * 60 + now.minute) >= 15 * 60 + 30:
            return False, f"장 마감 이후({now:%H:%M}) — 다음날 장 시작 전에 --login-now-wait 로 실행하세요", RC_OFFHOURS
        return True, "wait", 0
    if is_regular_session(now):
        return True, "", 0
    hm = now.hour * 60 + now.minute
    if now.weekday() < 5 and 7 * 60 <= hm < 9 * 60:
        return True, "wait", 0
    return (
        False,
        f"장외({now:%Y-%m-%d %H:%M}) — 평일 07:00 이후에 실행하거나, 더 이전이면 --login-now-wait 로 로그인만 먼저 하세요",
        RC_OFFHOURS,
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ok, reason, rc = preflight(sys.argv)
    if not ok:
        print(f"[stage05_regular_run] 거부: {reason}", flush=True)
        return rc
    os.environ["LIVE_ORDERS_ENABLED"] = "0"
    if not os.path.isfile(VERIFY):
        print(f"[stage05_regular_run] 검증 스크립트 없음: {VERIFY}", flush=True)
        return 2
    wait = WAIT_FLAG in sys.argv or reason == "wait"
    check = CHECK_FLAG in sys.argv or reason == "login-check"
    sys.path.insert(0, ROOT)
    import order_safety as osf

    lock = osf.InstanceLock()
    if not lock.acquire():
        print(
            "[stage05_regular_run] 거부: 이미 자동매매 앱(MyStock)이 실행 중입니다. "
            "앱을 종료한 뒤 다시 실행하세요. 키움은 한 세션만 가능합니다.",
            flush=True,
        )
        return RC_BUSY
    if check:
        msg = "로그인 확인만 합니다. 모의투자로 로그인되면 바로 종료합니다. 실시간 검증은 하지 않습니다."
        extra = ["--login-check"]
    elif wait:
        msg = (
            "지금 모의투자 로그인 → 09:00까지 대기 → 실시간 검증. 로그인 창이 뜨면 모의투자를 선택하세요. "
            "8시 MyStock 자동시작은 이 프로세스가 막고, 실패하는 것이 정상입니다. PC 절전을 끄세요."
        )
        extra = ["--wait-for-open"]
    else:
        msg = "정규장 즉시 검증. 키움 로그인 창이 뜨면 모의투자를 선택하세요."
        extra = []
    print(
        "[stage05_regular_run] LIVE_ORDERS_ENABLED=0, SendOrder 금지. " + msg,
        flush=True,
    )
    sys.argv = [VERIFY] + extra
    import runpy

    try:
        runpy.run_path(VERIFY, run_name="__main__")
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
