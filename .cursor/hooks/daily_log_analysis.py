#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cursor sessionStart hook — 20:00 이후 오늘 로그 분석 결과를 자동 주입.
stdin: JSON (hook event payload)
stdout: JSON { "user_message": "..." }  or  {}
"""
import json
import sys
import os
import datetime

def main():
    try:
        _input = json.load(sys.stdin)
    except Exception:
        _input = {}

    now = datetime.datetime.now()
    # 평일(월~금) 20:00 이후에만 실행
    if now.hour < 20:
        print("{}")
        return

    today_str = now.strftime("%Y-%m-%d")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log_dir = os.path.join(root, "log")

    analysis_path  = os.path.join(log_dir, f"{today_str}_analysis.txt")
    autofix_path   = os.path.join(log_dir, f"{today_str}_autofix.txt")
    strategy_path  = os.path.join(log_dir, f"{today_str}_strategy.txt")

    if not os.path.exists(analysis_path):
        print("{}")
        return

    def _read(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    analysis = _read(analysis_path)
    autofix  = _read(autofix_path)
    strategy = _read(strategy_path)

    # 핵심 수치만 추출 (간단 파싱)
    import re
    def _find(pattern, text, default="-"):
        m = re.search(pattern, text)
        return m.group(1) if m else default

    n_buy      = _find(r"합계\s+(\d+)건", analysis)
    n_sell     = _find(r"합계\s+\d+건\s+(\d+)건", analysis)
    n_filled   = _find(r"합계\s+\d+건\s+\d+건\s+(\d+)건", analysis)
    n_giveup   = _find(r"GIVE-UP (\d+)건", analysis)
    n_timeout  = _find(r"timeout 총 (\d+)회", analysis)
    top1_block = _find(r"1\. (.+?):", analysis)
    regime     = _find(r"시장 레짐: (\w+)", analysis)
    strategy_sel = _find(r"선택된 전략: \[.+?\] (.+)", strategy)

    # 긴급 플래그
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
        f"일일전략: {strategy_sel}\n\n"
        f"전체 분석 내용을 검토하고 필요한 조치를 안내해 주세요."
    )

    sys.stdout.buffer.write(
        json.dumps({"user_message": msg}, ensure_ascii=False).encode("utf-8") + b"\n"
    )


if __name__ == "__main__":
    main()
