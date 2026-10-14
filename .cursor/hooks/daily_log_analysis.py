#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cursor hook — 20:30 이후 오늘 로그 분석 결과를 자동 주입.

두 이벤트에서 호출된다:
  - sessionStart : 새 채팅을 시작할 때
  - stop         : 진행 중이던(이어지는) 대화의 턴이 끝날 때마다

2026-07-08 확인된 사실(Cursor 공식 문서 기준):
  - sessionStart 훅이 낼 수 있는 출력은 `additional_context`(모델 컨텍스트에만
    조용히 추가, 유저 화면에는 안 보임)와 `env` 뿐이다. `user_message`는 스키마상
    받아주기는 하지만 "현재 호출부가 강제하지 않는다"고 명시되어 있어 사실상
    무시된다 — 즉 지금까지 sessionStart에서 user_message로 보내던 요약은 유저
    화면에 절대 표시되지 않는 죽은 코드였다.
  - stop 훅의 `followup_message`만이 실제로 "다음 유저 메시지로 자동 제출"되어
    화면에 보이는 메시지가 된다.
  → sessionStart는 additional_context로 보내 모델(에이전트)이 세션 시작 시
    참고하게 하고, 실제 화면 표시는 stop 훅의 followup_message에만 의존한다.
    마커 파일도 stop이 실제로 표시에 성공했을 때만 기록해서, sessionStart의
    무의미한 호출이 stop의 정상 동작을 막지 않게 한다.

stdin: JSON (hook event payload)
stdout: JSON { "additional_context": "..." } / { "followup_message": "..." } / {}
"""
import json
import sys
import os
import datetime


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "sessionStart"

    try:
        _input = json.load(sys.stdin)
    except Exception:
        _input = {}

    now = datetime.datetime.now()
    if (now.hour, now.minute) < (20, 30):
        print("{}")
        return

    today_str = now.strftime("%Y-%m-%d")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log_dir = os.path.join(root, "log")

    analysis_path = os.path.join(log_dir, f"{today_str}_analysis.txt")
    autofix_path = os.path.join(log_dir, f"{today_str}_autofix.txt")
    strategy_path = os.path.join(log_dir, f"{today_str}_strategy.txt")
    marker_path = os.path.join(log_dir, f"{today_str}_daily_summary_notified.flag")

    if not os.path.exists(analysis_path):
        print("{}")
        return

    # stop 이벤트는 턴마다 반복 발동되므로, 오늘 이미 한 번 화면에 표시했으면
    # 더 이상 주입하지 않는다. sessionStart는 화면에 보이지 않는 조용한
    # additional_context라서 마커를 확인/기록하지 않는다(새 세션마다 매번 줘도 무해함).
    if event == "stop" and os.path.exists(marker_path):
        print("{}")
        return

    def _read(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    analysis = _read(analysis_path)
    autofix = _read(autofix_path)
    strategy = _read(strategy_path)
    reasoning = _read(os.path.join(log_dir, f"{today_str}_reasoning.txt"))
    reasoning_head = next(
        (l[3:].strip() for l in reasoning.splitlines() if l.startswith("한줄:")),
        "- (21:30 일일전략 후 생성)",
    )

    import re

    def _find(pattern, text, default="-"):
        m = re.search(pattern, text)
        return m.group(1) if m else default

    n_buy = _find(r"합계\s+(\d+)건", analysis)
    n_sell = _find(r"합계\s+\d+건\s+(\d+)건", analysis)
    n_filled = _find(r"합계\s+\d+건\s+\d+건\s+(\d+)건", analysis)
    n_giveup = _find(r"GIVE-UP (\d+)건", analysis)
    n_timeout = _find(r"timeout 총 (\d+)회", analysis)
    top1_block = _find(r"1\. (.+?):", analysis)
    regime = _find(r"시장 레짐: (\w+)", analysis)
    strategy_sel = _find(r"선택된 전략: \[.+?\] (.+)", strategy)

    alerts = []
    n_buy_int = int(n_buy) if n_buy.isdigit() else 0
    n_filled_int = int(n_filled) if n_filled.isdigit() else 0
    n_giveup_int = int(n_giveup) if n_giveup.isdigit() else 0
    if n_buy_int >= 5 and n_filled_int == 0:
        alerts.append(f"[긴급] 주문 {n_buy_int}건인데 체결 0건 -- 미체결 주문 확인 필요!")
    if n_giveup_int >= 2:
        alerts.append(f"[긴급] GIVE-UP {n_giveup_int}건 발생")

    alert_text = ("\n".join(alerts) + "\n\n") if alerts else ""

    msg = (
        f"{alert_text}"
        f"[자동분석] [{today_str}] 로그 분석 완료\n"
        f"매수 {n_buy}건 / 체결 {n_filled}건 / timeout {n_timeout}회 / GIVE-UP {n_giveup}건\n"
        f"시장: {regime} | 차단 1위: {top1_block}\n"
        f"자동조정: {autofix.splitlines()[6] if len(autofix.splitlines()) > 6 else '없음'}\n"
        f"일일전략: {strategy_sel}\n"
        f"추론: {reasoning_head}\n\n"
        f"전체 분석 내용을 검토하고 필요한 조치를 안내해 주세요."
    )

    if event == "stop":
        try:
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write(now.isoformat())
        except Exception:
            pass
        key = "followup_message"
    else:
        key = "additional_context"

    sys.stdout.buffer.write(
        json.dumps({key: msg}, ensure_ascii=False).encode("utf-8") + b"\n"
    )


if __name__ == "__main__":
    main()
