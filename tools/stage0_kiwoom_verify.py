"""
0단계 안전장치 — 실제 키움 OCX(모의투자) 검증 하네스.

절대 원칙
  * 실계좌/모의 가리지 않고 매수·매도 SendOrder 는 보내지 않는다.
    - 게이트 + 이 하네스의 dynamicCall 래퍼가 SendOrder 를 이중으로 막는다.
    - 예외: --allow-cancel 이고 '실제 미체결 주문이 조회된' 경우에 한해 그 주문의 취소만 허용.
  * LIVE_ORDERS_ENABLED=0 강제. 서버 구분이 모의("1")가 아니면 즉시 중단.
  * TradingEngine 은 만들지만 timer/start() 는 호출하지 않는다(장외 자동작업·매매 로직 미실행).
  * 로그/결과는 log/stage0_verify/ 에만 기록한다. 기존 로그·데이터는 삭제/수정하지 않는다.

사용:  python -X utf8 tools\\stage0_kiwoom_verify.py [--skip-disconnect] [--allow-cancel]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

os.environ["LIVE_ORDERS_ENABLED"] = "0"  # 무슨 일이 있어도 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL, NV = "PASS", "FAIL", "NOT VERIFIED"
RESULTS: list = []
SENDORDER_ATTEMPTS: list = []
VERIFIED_OPEN_ORDER_NOS: set = set()
FLAGS = {"allow_cancel": False, "blank_gubun": False}

STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = os.path.join(ROOT, "log", "stage0_verify")
os.makedirs(OUT_DIR, exist_ok=True)
LOG_PATH = os.path.join(OUT_DIR, f"stage0_verify_{STAMP}.txt")
JSON_PATH = os.path.join(OUT_DIR, f"stage0_verify_{STAMP}.json")


def out(msg: str) -> None:
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def rec(item: str, status: str, detail: str = "") -> None:
    RESULTS.append({"item": item, "status": status, "detail": detail})
    out(f"## {status:12s} {item} :: {detail}")
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"results": RESULTS, "sendorder_attempts": SENDORDER_ATTEMPTS}, f, ensure_ascii=False, indent=2)


def snapshot_files() -> dict:
    snap = {}
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__", "node_modules") and not d.startswith("backup")]
        if os.path.relpath(dp, ROOT).startswith(os.path.join("log", "stage0_verify")):
            continue
        for f in fn:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                snap[os.path.relpath(p, ROOT)] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    return snap


def mask(s: str) -> str:
    s = str(s or "")
    return s[:4] + "*" * max(0, len(s) - 4)


# ---------------------------------------------------------------------------
from PyQt5.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication(sys.argv)
try:
    import pythoncom  # type: ignore

    pythoncom.CoInitialize()
except Exception:
    pass


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec_()


def install_sendorder_guard(kiwoom) -> None:
    orig = kiwoom.dynamicCall

    def guarded(name, *a):
        n = str(name)
        if n.startswith("SendOrder"):
            args = list(a[0]) if a else []
            SENDORDER_ATTEMPTS.append(args)
            otype = int(args[3]) if len(args) > 3 else -1
            org = str(args[8]) if len(args) > 8 else ""
            if FLAGS["allow_cancel"] and otype in (3, 4) and org in VERIFIED_OPEN_ORDER_NOS:
                out(f"[GUARD] 취소 주문만 허용: type={otype} org={org}")
                return orig(name, *a)
            raise RuntimeError("HARNESS BLOCKED SendOrder (검증 하네스는 매수/매도 주문을 보내지 않는다)")
        if FLAGS["blank_gubun"] and n.startswith("GetLoginInfo") and a and str(a[0]) == "GetServerGubun":
            return ""
        return orig(name, *a)

    kiwoom.dynamicCall = guarded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-disconnect", action="store_true")
    ap.add_argument("--allow-cancel", action="store_true")
    ap.add_argument("--login-timeout", type=int, default=120)
    args = ap.parse_args()
    FLAGS["allow_cancel"] = bool(args.allow_cancel)

    before = snapshot_files()
    out(f"=== 0단계 키움 모의투자 검증 시작 {STAMP} (LIVE_ORDERS_ENABLED={os.environ['LIVE_ORDERS_ENABLED']}) ===")

    import order_safety as osf

    # ---- 중복 실행 방지: 이 프로세스가 실제 뮤텍스를 잡고, main.main() 이 거부하는지 -------------
    lock = osf.InstanceLock()
    if not lock.acquire():
        rec("⑥ 중복 실행: 기존 인스턴스 없음 확인", FAIL, "이미 다른 인스턴스가 뮤텍스를 점유 중 — 앱이 실행 중일 수 있어 검증 중단")
        return 2
    code = (
        "import sys,ctypes\n"
        "calls=[]\n"
        "ctypes.windll.user32.MessageBoxW=lambda *a: calls.append(a) or 0\n"
        "sys.argv=['main.py']\n"
        "import main\n"
        "try:\n"
        "    main.main()\n"
        "except SystemExit as e:\n"
        "    print('EXITCODE',e.code,'MSGBOX',len(calls),flush=True)\n"
    )
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180,
                           env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        tail = [ln for ln in p.stdout.splitlines() if "EXITCODE" in ln]
        ok = bool(tail) and "EXITCODE 2" in tail[-1]
        rec("⑥ 두 번째 실행 차단: 실제 main.main() E2E (로그인 전 단계)", PASS if ok else FAIL,
            tail[-1] if tail else f"종료 신호 없음 rc={p.returncode} out={p.stdout[-200:]!r}")
    except Exception as e:  # noqa: BLE001
        rec("⑥ 두 번째 실행 차단: 실제 main.main() E2E", FAIL, f"{type(e).__name__}: {e}")
    finally:
        lock.release()

    import main as m

    # ---- ① 로그인 / 서버 구분 ----------------------------------------------------
    kiwoom = m.KiwoomOpenAPI()
    if kiwoom.isNull():
        rec("① 키움 OCX 로드", FAIL, "KHOPENAPI 컨트롤 로딩 실패(isNull)")
        return 3
    rec("① 키움 OCX 로드", PASS, "KHOPENAPI.KHOpenAPICtrl.1 로드됨")
    install_sendorder_guard(kiwoom)
    gate = osf.reset_gate_for_tests(osf.OrderSafetyGate())  # env 의 LIVE_ORDERS_ENABLED=0 사용
    kiwoom.safety_gate = gate

    out(f"로그인 시도(최대 {args.login_timeout}s) — 키움 로그인 창이 뜨면 로그인해 주세요")
    try:
        account = kiwoom.login(timeout_ms=args.login_timeout * 1000)
    except Exception as e:  # noqa: BLE001
        rec("① 키움 로그인", NV, f"로그인 완료 못함: {type(e).__name__}: {e} (사용자 로그인 필요/서버 점검 가능)")
        return 4
    state = int(kiwoom.dynamicCall("GetConnectState()") or 0)
    gubun = str(kiwoom.dynamicCall("GetLoginInfo(QString)", "GetServerGubun") or "").strip()
    accs = str(kiwoom.dynamicCall("GetLoginInfo(QString)", "ACCNO") or "")
    rec("① 키움 로그인", PASS if state == 1 else FAIL,
        f"GetConnectState={state}, 계좌={mask(account)}, 계좌목록 {len([a for a in accs.split(';') if a.strip()])}개")
    is_mock = gubun == "1"
    rec("① 서버 구분(GetServerGubun)", PASS if is_mock else FAIL,
        f"GetServerGubun='{gubun}' → {'모의투자' if is_mock else '모의 아님'}; kiwoom.is_mock_server={kiwoom.is_mock_server}, server_gubun='{kiwoom.server_gubun}'")
    if not is_mock:
        rec("중단", FAIL, "모의투자 서버가 아니므로 이후 검증 전체 중단 (실계좌 보호)")
        return 5

    # 게이트 판정(실제 probe) — SendOrder 는 호출하지 않는다
    d = gate.authorize(osf.KIND_BUY, "005930", server_is_mock=osf.probe_server_is_mock(kiwoom),
                       connected=osf.probe_connected(kiwoom))
    rec("① 모의서버+연결 정상일 때 게이트 판정(실제 probe)", PASS if d.allowed else FAIL,
        f"allowed={d.allowed} reason={d.reason} (모의 기존 동작 유지 확인, 주문은 보내지 않음)")

    # 서버 구분 확인 실패 시 차단: 실제 kiwoom.send_order_market 경로
    n0 = len(SENDORDER_ATTEMPTS)
    FLAGS["blank_gubun"] = True
    saved = kiwoom.server_gubun
    kiwoom.server_gubun = ""
    try:
        r = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
    finally:
        FLAGS["blank_gubun"] = False
        kiwoom.server_gubun = saved
    rec("① 서버 구분 확인 실패 시 주문 차단(실제 send_order_market)",
        PASS if (r.status == osf.S_BLOCKED and r.reason == osf.R_SERVER_UNKNOWN and len(SENDORDER_ATTEMPTS) == n0) else FAIL,
        f"status={r.status} reason={r.reason} SendOrder시도 증가={len(SENDORDER_ATTEMPTS) - n0}")

    # ---- 실시간 시세 -------------------------------------------------------------
    ticks: list = []
    kiwoom.on_real_price = lambda c, px: ticks.append((c, px))
    codes = ["005930", "000660"]
    try:
        kiwoom.set_real_reg(codes, "10")
        out("실시간 체결(FID10) 등록 완료 — 20초 대기")
        pump(20000)
        kiwoom.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        sess = "정규장" if (datetime.datetime.now().weekday() < 5 and 9 <= datetime.datetime.now().hour < 15) else "장외"
        if ticks:
            rec("② 실시간 체결 수신", PASS, f"{len(ticks)}건 수신, 예: {ticks[:2]}")
        else:
            rec("② 실시간 체결 수신", NV, f"등록 호출은 예외 없이 완료, 20초간 수신 0건 — 현재 {sess}({datetime.datetime.now():%H:%M})라 체결 이벤트가 없는 것이 정상. 장중 재검증 필요")
    except Exception as e:  # noqa: BLE001
        rec("② 실시간 체결 등록", FAIL, f"{type(e).__name__}: {e}")

    px = 0
    try:
        px = kiwoom.request_current_price("005930")
        rec("② 현재가 TR(opt10001) 응답", PASS if px > 0 else FAIL, f"005930 현재가(장외라 종가) = {px}")
    except Exception as e:  # noqa: BLE001
        rec("② 현재가 TR(opt10001) 응답", FAIL, f"{type(e).__name__}: {e}")

    # ---- 잔고 TR -----------------------------------------------------------------
    pw = os.environ.get("KIWOOM_ACCOUNT_PASSWORD", "")
    t0 = kiwoom.last_holdings_ok_at
    h = kiwoom.request_holdings(account, password=pw, timeout_ms=20000)
    rec("③ 잔고 조회 TR(opw00018) 응답", PASS if (h is not None and kiwoom.last_holdings_ok_at > t0) else FAIL,
        f"rows={None if h is None else len(h)}, 비번={'설정' if pw else '미설정(0행이어도 TR 응답 자체는 성공으로 본다)'}, last_holdings_ok_at 갱신={kiwoom.last_holdings_ok_at > t0}")

    # ---- ④ opt10075 -----------------------------------------------------------------
    kiwoom._last_error = None
    orders = kiwoom.request_open_orders(account)
    out(f"opt10075 직후 OnReceiveMsg: {kiwoom._last_error}")
    if orders is None:
        rec("④ opt10075 조회 요청/응답", FAIL, f"None 반환(미수신/오류). 서버 메시지={kiwoom._last_error}")
    else:
        rec("④ opt10075 조회 요청/응답", PASS, f"응답 수신, 미체결 {len(orders)}건. 서버 메시지={kiwoom._last_error}")
        if orders:
            for o in orders:
                out(f"   미체결: {o}")
            ok_parse = all(o.code.isdigit() and len(o.code) == 6 and o.order_no and o.side in ("BUY", "SELL") for o in orders)
            rec("④ 미체결 데이터 파싱", PASS if ok_parse else FAIL, f"{len(orders)}건 필드 확인")
            VERIFIED_OPEN_ORDER_NOS.update(o.order_no for o in orders)
        else:
            rec("④ 미체결 데이터 파싱", NV, "미체결 0건이라 필드 파싱을 확인할 수 없음 (주문을 내지 않는 원칙상 장중 실제 미체결이 생겼을 때 재검증)")

    # 조회 실패 경로(실제 TR 을 1ms 만 기다려 의도적으로 타임아웃)
    r_fail = kiwoom.request_open_orders(account, timeout_ms=1)
    pump(3000)  # 늦게 도착하는 응답 소진
    rec("④ 조회 실패 경로(실제 TR 타임아웃)", PASS if r_fail is None else NV,
        "1ms 대기 → None 반환(실패로 인식)" if r_fail is None else "1ms 안에 응답이 와 실패 경로를 만들지 못함")

    # 취소 주문
    if orders and args.allow_cancel:
        for o in orders:
            if not o.cancellable():
                continue
            res = kiwoom.cancel_order(account, o)
            rec("④ 취소 주문 요청 반환값", PASS if res.accepted else FAIL,
                f"order_no={o.order_no} status={res.status} ret={res.ret_code} ({res.detail})")
        pump(3000)
        after = kiwoom.request_open_orders(account)
        rec("④ 취소 요청 ≠ 취소 완료: 재조회", PASS if after is not None else FAIL,
            f"요청 후 잔존 미체결 {None if after is None else len(after)}건")
    elif orders:
        rec("④ 취소 주문 요청", NV, f"미체결 {len(orders)}건이 있으나 --allow-cancel 미지정이라 취소하지 않음")
    else:
        rec("④ 취소 주문 요청/반환값", NV, "취소할 실제 미체결 주문이 없음(주문을 내지 않으므로 검증 불가)")

    # ---- ② GUI + 실제 엔진(타이머 미시작) -----------------------------------------------
    from PyQt5.QtCore import QTimer as _QT

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)  # 확인창 자동 '예'
    w = m.TradingWindow()
    w.resize(1500, 950)
    w.show()
    pump(500)
    engine = m.TradingEngine(kiwoom, account, [])
    vdir = os.path.join(OUT_DIR, "engine_artifacts")
    os.makedirs(vdir, exist_ok=True)
    engine.order_event_csv_path = os.path.join(vdir, "order_event_log_verify.csv")  # 실제 order_event_log.csv 오염 방지
    engine._write_log_file = lambda msg: open(LOG_PATH, "a", encoding="utf-8").write(f"[ENGINE] {msg}\n")
    engine._account_password = pw
    engine.on_log = w._append_log
    engine.timer = _QT()          # on_tick 에 연결되지 않은 더미 타이머(정지/재개 버튼 동작 확인용)
    engine.timer.start(60000)
    w.engine = engine
    w.kiwoom = kiwoom
    w.refresh_ui()
    pump(300)
    rec("② GUI+실제 키움 객체 생성", PASS, f"label='{w.lbl_safety.text()}'")

    def blocked(res, reason):
        return res.status == osf.S_BLOCKED and res.reason == reason

    n0 = len(SENDORDER_ATTEMPTS)
    w.btn_block_buys.click(); pump(200)
    rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
    sell_d = gate.authorize(osf.KIND_SELL, "005930", server_is_mock=True, connected=True)
    rec("② 신규매수 차단 버튼(실제 키움 객체)",
        PASS if (gate.block_buys and blocked(rb, osf.R_BLOCK_BUY) and sell_d.allowed and "매수차단" in w.lbl_safety.text()) else FAIL,
        f"매수→{rb.status}/{rb.reason}, 매도 게이트 허용={sell_d.allowed}, label='{w.lbl_safety.text()}', 버튼='{w.btn_block_buys.text()}'")
    w.btn_block_buys.click(); pump(200)

    w.btn_block_all.click(); pump(200)
    rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
    rs = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_SELL)
    rec("② 주문 전체 차단 버튼(실제 키움 객체)",
        PASS if (blocked(rb, osf.R_BLOCK_ALL) and blocked(rs, osf.R_BLOCK_ALL) and "주문전체차단" in w.lbl_safety.text()) else FAIL,
        f"매수→{rb.status}/{rb.reason}, 매도→{rs.status}/{rs.reason}, label='{w.lbl_safety.text()}'")
    w.btn_block_all.click(); pump(200)

    # 정지 vs 긴급정지
    w.btn_stop.click(); pump(200)
    stop_ok = (not engine.timer.isActive()) and not (gate.emergency or gate.block_all or gate.block_buys)
    w.btn_emergency.click(); pump(1500)   # 실제 opt10075 조회 + 상태 JSON 기록
    rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
    snaps = [f for f in os.listdir(os.path.join(vdir, "safety"))] if os.path.isdir(os.path.join(vdir, "safety")) else []
    emerg_ok = gate.emergency and blocked(rb, osf.R_EMERGENCY) and (not engine.timer.isActive()) and bool(snaps)
    rec("② 일반 정지 vs 긴급정지 구분(실제 엔진 메서드, 더미 타이머)",
        PASS if (stop_ok and emerg_ok) else FAIL,
        f"정지: 타이머 정지·게이트 불변={stop_ok}; 긴급정지: 게이트 emergency={gate.emergency}, 매수→{rb.reason}, 타이머 그대로 정지={not engine.timer.isActive()}, 상태JSON={snaps}, label='{w.lbl_safety.text()}', 정지버튼='{w.btn_stop.text()}', 긴급버튼='{w.btn_emergency.text()}'")
    rec("④ 긴급정지 시 실제 미체결 조회", PASS if any("미체결" in x for x in [w.log.toPlainText()]) else FAIL,
        "GUI 로그: " + " | ".join(ln for ln in w.log.toPlainText().splitlines() if "긴급정지" in ln and "미체결" in ln)[:200])
    w.btn_emergency.click(); pump(200)  # 해제
    w.btn_stop.click(); pump(200)       # 재개
    rec("② 긴급정지 해제/재개 버튼", PASS if (not gate.emergency and engine.timer.isActive()) else FAIL,
        f"emergency={gate.emergency}, timer={engine.timer.isActive()}")
    rec("⑤ 이 구간 SendOrder 실제 호출", PASS if len(SENDORDER_ATTEMPTS) == n0 else FAIL,
        f"하네스가 감지한 SendOrder 시도 증가={len(SENDORDER_ATTEMPTS) - n0}")

    # ---- ③ 재동기화 규칙(게이트 상태를 수동 재현 + 실제 TR) -------------------------------
    gate.observe_connection(True); gate.observe_connection(False); gate.observe_connection(True)
    rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
    rec("③ 복구 직후 동기화 전 주문 차단(실제 probe, 단절은 수동 재현)", PASS if blocked(rb, osf.R_RESYNC_PENDING) else FAIL,
        f"매수→{rb.status}/{rb.reason}")
    # 미체결 조회 실패를 결정적으로 재현: 실제 TR 은 수행하되 응답을 폐기하고 None(=조회 실패)을 돌려준다.
    # (1ms 타임아웃 방식은 응답이 먼저 도착하는 경우가 있어 비결정적이었음 → 첫 실행에서 하네스 결함으로 확인)
    # 이 구간에서는 게이트가 풀릴 수 있으므로 send_order_market 대신 authorize 로만 판정한다.
    real_req = kiwoom.request_open_orders

    def _dropped(acc, timeout_ms=0):
        real_req(acc)      # 실제 opt10075 TR 수행
        return None        # 응답 폐기 → 호출자에게는 '조회 실패'

    kiwoom.request_open_orders = _dropped
    engine._run_safety_resync_deferred()
    pump(500)
    still = gate.resync_required
    d = gate.authorize(osf.KIND_BUY, "005930", server_is_mock=True, connected=osf.probe_connected(kiwoom))
    rec("④ 미체결 조회 실패(실제 TR 수행 후 응답 폐기) 시 주문 재개 방지", PASS if (still and (not d.allowed) and d.reason == osf.R_RESYNC_PENDING) else FAIL,
        f"재확인 후 resync_required={still}, 게이트 판정={d.allowed}/{d.reason} (실패는 주입, 키움 객체·TR·엔진·게이트는 실제)")
    kiwoom.request_open_orders = real_req
    engine._run_safety_resync_deferred()
    pump(500)
    d = gate.authorize(osf.KIND_BUY, "005930", server_is_mock=True, connected=osf.probe_connected(kiwoom))
    rec("③ 잔고+미체결 둘 다 성공 후 주문 재개(실제 TR)", PASS if (not gate.resync_required and d.allowed) else FAIL,
        f"resync_required={gate.resync_required}, 게이트 허용={d.allowed}/{d.reason}")

    # ---- ③ 실제 연결 끊김/복구 (CommTerminate 시도) ---------------------------------------
    if args.skip_disconnect:
        rec("③ 실제 API 연결 끊김/복구", NV, "--skip-disconnect 지정")
    else:
        out("CommTerminate() 로 실제 연결 끊김 유도 시도")
        try:
            kiwoom.dynamicCall("CommTerminate()")
        except Exception as e:  # noqa: BLE001
            out(f"CommTerminate 예외: {e}")
        pump(5000)
        st2 = int(kiwoom.dynamicCall("GetConnectState()") or 0)
        out(f"CommTerminate 후 GetConnectState={st2}")
        if st2 != 0:
            rec("③ 실제 API 연결 끊김 유도", NV, f"CommTerminate 후에도 GetConnectState={st2} — OpenAPI 가 이 함수로 연결을 끊지 않음. 실제 단절은 네트워크 차단 등 외부 조작이 필요")
        else:
            engine._safety_on_tick(False)
            rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
            rs = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_SELL)
            rec("③ 실제 단절 시 신규 주문 차단", PASS if (blocked(rb, osf.R_DISCONNECTED) and blocked(rs, osf.R_DISCONNECTED)) else FAIL,
                f"매수→{rb.reason}, 매도→{rs.reason}, resync_required={gate.resync_required}")
            try:
                kiwoom.login(timeout_ms=args.login_timeout * 1000)
                st3 = int(kiwoom.dynamicCall("GetConnectState()") or 0)
                rb = kiwoom.send_order_market(account, "005930", 1, osf.ORDER_TYPE_BUY)
                rec("③ 재로그인 직후(동기화 전) 주문 차단", PASS if (st3 == 1 and blocked(rb, osf.R_RESYNC_PENDING)) else FAIL,
                    f"GetConnectState={st3}, 매수→{rb.reason}")
                engine._safety_on_tick(True)
                pump(4000)
                d = gate.authorize(osf.KIND_BUY, "005930", server_is_mock=True, connected=osf.probe_connected(kiwoom))
                rec("③ 실제 복구 후 잔고·미체결 재확인 → 주문 재개", PASS if (not gate.resync_required and d.allowed) else FAIL,
                    f"resync_required={gate.resync_required}, 게이트 허용={d.allowed}")
            except Exception as e:  # noqa: BLE001
                rec("③ 재로그인/복구", NV, f"재로그인 완료 못함: {type(e).__name__}: {e}")

    rec("⑤ SendOrder 반환값 기록 / 0≠체결 / 실패 쿨다운 / 중복 주문", NV,
        "매수·매도 주문을 보내지 않는 원칙상 실제 키움 SendOrder 경로는 검증하지 않음 (자동 테스트의 가짜 객체 결과만 존재)")
    rec("전체 SendOrder 시도(하네스 감지)", PASS if not SENDORDER_ATTEMPTS else FAIL,
        f"시도 {len(SENDORDER_ATTEMPTS)}건: {SENDORDER_ATTEMPTS}")

    after = snapshot_files()
    changed = sorted(k for k in after if k not in before or after[k] != before[k])
    removed = sorted(k for k in before if k not in after)
    rec("파일 변화(기존 데이터/로그 삭제 여부)", PASS if not removed else FAIL,
        f"삭제 {len(removed)}건 {removed[:5]}, 신규/변경 {len(changed)}건 {changed[:25]}")
    w.close()
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        import traceback

        rec("하네스 예외", FAIL, traceback.format_exc()[-800:])
    out(f"=== 종료 rc={rc} 결과 {len(RESULTS)}건 → {JSON_PATH} ===")
    os._exit(rc if isinstance(rc, int) else 1)
