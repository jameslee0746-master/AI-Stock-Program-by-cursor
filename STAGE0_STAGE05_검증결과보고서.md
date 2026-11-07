# 0단계 · 0.5단계 검증 결과 보고서

- 작성일: 2026-11-05
- 대상: 키움 Open API+ OCX 자동매매 프로그램의 안전장치(0단계)와 실시간 데이터·연결 복구 검증(0.5단계)
- 검증 환경: 키움 **모의투자 서버**(`GetServerGubun='1'`), Python 3.9 32bit, Windows
- 공통 조건: `LIVE_ORDERS_ENABLED=0` 유지 / 실제 SendOrder 0건 / 기존 데이터·로그 삭제 0건 / 기존 전략·매매 로직 변경 없음

> **구분 안내.** 이 문서의 "실제 키움 검증"은 키움 OCX를 실제로 띄워 모의서버에서 얻은 결과이고, "자동 테스트"는 가짜 객체로 돌린 단위 테스트입니다. 둘을 같은 것으로 취급하지 않습니다.

---

## 1. 요약

| 구분 | 실행 시각 | 결과 |
|---|---|---|
| 자동 테스트 (0단계) | 2026-11-04 | 65건 모두 통과 (`test_order_safety` 44 + `test_gui_safety` 21) |
| 자동 테스트 (0.5단계 분석 함수) | 2026-11-04 | 8건 모두 통과 (`test_stage05_analysis`) |
| 실제 키움 검증, 0단계 (최종 실행) | 2026-11-04 23:26 (장외) | 27건 중 **PASS 22 / NOT VERIFIED 5 / FAIL 0** |
| 실제 키움 검증, 0.5단계 (장외 리허설) | 2026-11-04 23:38 (장외) | 26건 중 **PASS 10 / NOT VERIFIED 16 / FAIL 0** — 정식 결과 아님 |
| 실제 키움 검증, 0.5단계 (정식, 장중) | **미실행 (다음 정규장 대기)** | FID 10/20·호가·FID 228 수신, opt10075 파싱은 아직 장중에서 확인되지 않음. 단절 테스트는 별도 승인 전 제외 |

**이번 단계 목표:** 수익이 아니라, 키움 실시간 데이터가 정확하게 들어오고 주문 안전장치가 주문을 막는지 확인하는 것입니다.

**결론: 실계좌 전환과 다음 HFT 개발 단계는 아직 진행하지 않습니다.** 장중 수신 검증이 끝나기 전에는 데이터 수집·전략·시뮬레이터 단계로 넘어가지 않습니다.

---

## 2. 0단계 — 실제 키움 모의투자 검증 (2026-11-04 23:26:50)

결과 파일: `log/stage0_verify/stage0_verify_20261104_232650.txt` / `.json`

| 항목 | 판정 | 근거 |
|---|---|---|
| 키움 OCX 로드 | PASS | `KHOPENAPI.KHOpenAPICtrl.1` 로드 |
| 로그인 | PASS | `GetConnectState=1`, 계좌 1개 |
| 서버 구분 | PASS | `GetServerGubun='1'`(모의) |
| 모의서버+연결 정상 시 게이트 판정 | PASS | allowed=True, 주문은 보내지 않음 |
| 서버 구분 확인 실패 시 주문 차단 | PASS | `SERVER_UNKNOWN`으로 차단, SendOrder 시도 0 |
| 실시간 체결 수신 | NOT VERIFIED | 등록은 성공, 장외라 20초간 수신 0건 |
| 현재가 TR(opt10001) | PASS | 005930 응답 수신 |
| GUI 생성 + 안전 라벨 | PASS | "안전: 모의서버 \| 차단 없음" |
| 신규매수 차단 버튼 | PASS | 매수 차단·매도 허용, 라벨·버튼 문구 변경 |
| 주문 전체 차단 버튼 | PASS | 매수·매도 모두 `BLOCK_ALL` |
| 일반 정지 vs 긴급정지 | PASS | 정지는 게이트 불변, 긴급정지는 emergency 설정·매수 차단·상태 JSON 저장 |
| 긴급정지 해제/재개 | PASS | emergency 해제, 타이머 재개 |
| 긴급정지 시 미체결 조회 | PASS | 실제 조회 후 "미체결 0건 확인" 기록 |
| 잔고 조회 TR(opw00018) | PASS | 응답 수신, `last_holdings_ok_at` 갱신 |
| opt10075 조회 요청/응답 | PASS | 응답 수신, 미체결 0건 |
| 미체결 데이터 파싱 | NOT VERIFIED | 0건이라 확인 불가 |
| 조회 실패 경로 | PASS | 실제 TR 타임아웃(1ms) → `None` 처리 |
| 조회 실패 시 주문 재개 방지 | PASS | 실제 TR 후 응답 폐기 → `RESYNC_PENDING` 유지 (실패는 주입) |
| 복구 직후 동기화 전 주문 차단 | PASS | `RESYNC_PENDING` (단절은 수동 재현) |
| 잔고+미체결 둘 다 성공 후 주문 재개 | PASS | `resync_required=False`, 허용 |
| 취소 주문 요청/반환값 | NOT VERIFIED | 취소할 실제 미체결 없음 |
| 실제 API 연결 끊김/복구 | NOT VERIFIED | `--skip-disconnect` 지정. `CommTerminate()`는 연결을 끊지 않음(상태 1 유지) |
| SendOrder 반환값 기록, 0≠체결, 실패 쿨다운, 중복 주문 | NOT VERIFIED | 주문을 보내지 않는 원칙상 실제 경로 미검증(자동 테스트 결과만 존재) |
| 두 번째 실행 차단 (중복 실행 방지) | PASS | 실제 `main.main()`이 종료코드 2, 메시지박스 1회 |
| 전체 SendOrder 시도 | PASS | 0건 |
| 파일 삭제 여부 | PASS | 삭제 0건, 변경 1건(`waiting_status_log.csv`, GUI 갱신으로 1행 추가) |

### 첫 실행(23:24:05)과의 관계
첫 실행에서 FAIL 2건과 SendOrder 시도 1건이 있었습니다. 원인은 **제품 코드가 아니라 검증 스크립트의 결함**이었습니다. 1ms 타임아웃으로 조회 실패를 만들려 했는데 실패가 매번 같게 나지 않아, 게이트가 풀린 상태에서 스크립트가 주문 함수를 호출했습니다. 그 시도는 스크립트의 가드가 키움에 닿기 전에 막았고 실제 주문은 전송되지 않았습니다. 스크립트만 수정(실패 주입을 고정하고 게이트 판정으로만 확인)했고, 최종 실행에서 해소되었습니다.
파일: `stage0_verify_20261104_232405.txt` / `.json`

---

## 3. 0.5단계 — 장외 리허설 (2026-11-04 23:38:37)

결과 파일: `log/stage0_verify/stage05_verify_20261104_233837.txt` / `.json`

> 장외에 `--force-offhours`로 실행한 **리허설**입니다. 등록 호출과 조회 경로가 동작하는지만 확인했으며, 정식 장중 결과로 쓰면 안 됩니다.

| 항목 | 판정 | 근거 |
|---|---|---|
| 로그인 / 서버 구분 | PASS | 모의 `'1'` |
| FID 10+20 등록 (기존 `set_real_reg` 구조) | PASS | `SetRealReg` 반환 0 |
| FID 10 실제 수신 | NOT VERIFIED | 등록 성공, 15초간 수신 0건(장외) |
| FID 20 실제 수신 | NOT VERIFIED | 체결 이벤트 없음 |
| FID 228(체결강도) 등록 | PASS | 반환 0 |
| FID 228 실제 수신 | NOT VERIFIED | 체결 이벤트 0건 |
| 호가·잔량 FID 23개 등록 | PASS | 반환 0 (FID 21, 41~45, 51~55, 61~65, 71~75, 121, 125) |
| 매도·매수 호가, 호가수량, 총잔량 실제 수신 | NOT VERIFIED | 호가 이벤트 0건 |
| opt10075 조회 응답 | PASS | 응답 수신, 반복 데이터 0건 |
| 필드 6종(주문번호, 종목코드, 주문수량, 미체결수량, 주문가격, 매매구분) | NOT VERIFIED | 0건이면 파싱 성공으로 판정하지 않음 |
| 제품 파서(`normalize_open_order`) 일치 | NOT VERIFIED | 0건 |
| 취소 주문 | NOT VERIFIED | 0.5단계는 취소 주문을 보내지 않음 |
| SendOrder 호출 횟수 | PASS | 0건 |
| `LIVE_ORDERS_ENABLED` | PASS | 0 |
| 파일 삭제 여부 | PASS | 삭제 0, 변경 0 |

---

## 4. 0.5단계 최종 보고 10항목 (2026-11-05 22:21 기준, 장중 미실행)

장외 리허설 수치를 장중 PASS로 바꾸지 않습니다. 정규장 실행 후에만 ①~⑤를 갱신합니다.

| # | 항목 | 현재 결과 |
|---|---|---|
| 1 | FID 10 수신 건수 | **NOT VERIFIED** — 장중 미실행. 장외 리허설은 등록만 성공, 수신 0건 |
| 2 | FID 20 수신 건수 | **NOT VERIFIED** — 장중 미실행 |
| 3 | 호가 데이터 수신 건수 | **NOT VERIFIED** — 장중 미실행. 장외 리허설은 등록만 성공, 수신 0건 |
| 4 | FID 228 수신 건수 | **NOT VERIFIED** — 장중 미실행. 장외 리허설은 등록만 성공, 수신 0건 |
| 5 | opt10075 응답 및 필드 파싱 | 응답 수신은 장외에서 PASS. 미체결 0건이라 필드 파싱은 **NOT VERIFIED** (0건을 파싱 성공으로 보지 않음) |
| 6 | 연결 상태 및 복구 결과 | **이번 정규장 범위 밖.** 단절 테스트는 별도 승인 전까지 금지. 기록은 NOT VERIFIED |
| 7 | SendOrder 호출 횟수 | **0건** (장외 리허설 기준). 정규장 실행에서도 0이어야 함 |
| 8 | 변경 파일 목록 | 제품 코드(`main.py`) 변경 없음. 정규장 실행기 `tools/stage05_regular_run.py` 신규 |
| 9 | 발견된 문제 | 장중 미실행이 현재 유일한 차단 요인. 장외 강제 실행은 하지 않음 |
| 10 | 다음 HFT 개발 단계 진행 가능 여부 | **불가.** 정규장에서 ①~④가 확인되기 전에는 넘어가지 않음 |

---

## 5. 변경된 파일

### 0단계 (2026-11-04)
| 파일 | 구분 | 이유 |
|---|---|---|
| `order_safety.py` | 신규 | 주문 차단 게이트, 긴급정지, 중복 실행 방지(순수 Python) |
| `main.py` | 수정(약 +490/−20줄) | 주문 경로를 게이트로 일원화, GUI 안전 버튼·라벨, 시작 시 점검. 매매 전략·조건은 변경 없음 |
| `tests/test_order_safety.py`, `tests/test_gui_safety.py` | 신규 | 자동 테스트 65건 |
| `tools/stage0_kiwoom_verify.py` | 신규 | 실제 키움 모의서버 검증 스크립트 |
| `STAGE0_안전장치_변경기록.md` | 신규 | 변경 기록과 남은 위험 요소 |

### 0.5단계 (2026-11-04 ~ 2026-11-05)
| 파일 | 구분 | 이유 |
|---|---|---|
| `tools/stage05_kiwoom_verify.py` | 신규 | 실시간 수신 / 미체결 구조 / 단절·복구 검증 스크립트. SendOrder·취소·`CommTerminate`는 코드에서 차단 |
| `tools/stage05_regular_run.py` | 신규 | 정규장 전용 실행기. 장외·`--force-offhours`·disconnect 거부. `LIVE_ORDERS_ENABLED=0` 강제 |
| `tests/test_stage05_analysis.py` | 신규 | 분석 함수 자동 테스트 8건 |
| `tests/test_stage05_regular_run.py` | 신규 | 실행기 가드 테스트 (키움 미접속) |
| `.cursor/rules/stage05-regular-session.mdc` | 신규 | 장중 검증 전 HFT 단계 착수 금지 |

`main.py`는 0.5단계에서 수정하지 않았습니다. 백업: `C:\cursorAI_work_backup\pre_stage0_20261104_224113\`.

---

## 6. 발견 사항 및 알려진 한계

1. **검증 스크립트 결함(해결됨):** 2장의 첫 실행 참조. 제품 코드 문제가 아님.
2. **로그 중복(미수정):** `[SAFETY]` 로그와 긴급정지 보고가 GUI와 엔진에서 두 번씩 기록됩니다. 기능 문제는 아닙니다.
3. **`waiting_status_log.csv`:** GUI 갱신으로 1행이 추가됐습니다. 삭제는 없습니다.
4. **opt10075 `시간` 필드(미확인 가정):** 제품 파서는 `시간` 필드를 읽습니다. 키움 문서상 이 필드명은 `주문/체결시간`일 수 있어 값이 비어 있을 가능성이 있습니다. 미체결이 실제로 있을 때 0.5단계 스크립트가 두 필드를 함께 비교합니다. 확인 전까지는 가정입니다.
5. **단절 감지 방식(미확인 가정):** 앱 게이트는 `GetConnectState`만 보고 단절을 판단합니다. 네트워크가 끊겨도 이 값이 바로 바뀌지 않으면 단절을 인지하지 못할 수 있습니다. 정식 단절 검증에서 확인할 항목입니다.
6. **재시작 시 상태:** 안전장치 상태(차단 등)는 재시작하면 유지되지 않습니다. 재시작 후 미체결은 조회·기록만 하고 복원하지 않습니다.
7. **손절:** 앱이 시세를 보고 있는 동안만 동작합니다. 증권사 쪽 손절 주문은 없습니다.
8. **`LIVE_ORDERS_ENABLED=0`:** 실서버에서는 취소 주문도 막힙니다.
9. **예약 작업:** `MyStockOfcursor` 작업의 마지막 결과 코드가 2147943467이었습니다. 이번 검증과 무관해 보여 조사하지 않았습니다.

---

## 7. 다음 정규장 실행 준비 (2026-11-06 09:00~15:30)

09시 이후 자리에 없으면, **8시 전에** 로그인만 먼저 하고 09:00까지 기다리게 합니다. `--force-offhours`와 `--phases disconnect`는 넣지 않습니다.

```powershell
python -X utf8 tools\stage05_regular_run.py --login-now-wait
```

이미 정규장이고 자리에 있으면:

```powershell
python -X utf8 tools\stage05_regular_run.py
```

실행기가 하는 일:
- 평일 09:00~15:30이 아니면 키움에 접속하지 않고 종료 코드 20으로 거부
- `--force-offhours` / `disconnect` / `--phases` 가 있으면 종료 코드 21로 거부
- `LIVE_ORDERS_ENABLED=0` 강제 후 `stage05_kiwoom_verify.py`를 `realtime,orders`만 실행
- 로그인 창이 뜨면 **모의투자**를 선택

실행 전 체크:
1. 기존 `main.py` 앱 종료 (키움 로그인은 한 세션만 가능)
2. `log`·`data`는 복사만 하고 삭제하지 않음
3. 제품 코드 수정 금지, `watchdog_runner.py` 실행 금지
4. FID 10 수신이 0건이면 멈추고 원인을 먼저 보고 (임의 수정 금지)

포함: ① FID 10·20 수신, ② 호가, ③ FID 228, ④ opt10075 필드 파싱.  
제외: ⑤ 연결 단절·복구 (별도 승인 전). GUI 안전장치는 0단계에서 이미 모의서버 PASS.

판정: 각 항목 PASS / FAIL / NOT VERIFIED. 미체결 0건이면 파싱을 PASS로 쓰지 않음.  
결과: `log/stage0_verify/stage05_verify_날짜시간.txt` / `.json`  
소요: 로그인 포함 약 3~5분. SendOrder는 0건이어야 함.

HFT 다음 단계(데이터 수집 등)로 넘어가는 조건: 위 ①~④가 장중에서 확인되고 FAIL이 없을 것. 단절 테스트는 그 조건에 넣지 않습니다(승인 전 금지).

---

## 8. 결과 파일 위치

| 구분 | 경로 |
|---|---|
| 0단계 최종 | `log/stage0_verify/stage0_verify_20261104_232650.txt`, `.json` |
| 0단계 첫 실행 | `log/stage0_verify/stage0_verify_20261104_232405.txt`, `.json` |
| 0.5단계 리허설 (정식 아님) | `log/stage0_verify/stage05_verify_20261104_233837.txt`, `.json` |
| 0.5단계 정규장 결과 | 다음 정규장 실행 후 `log/stage0_verify/stage05_verify_<실행시각>.*` |
| 정규장 실행기 | `tools/stage05_regular_run.py` |
| 긴급정지 상태 스냅샷 | `log/stage0_verify/engine_artifacts/safety/emergency_stop_20261104_232507.json`, `emergency_stop_20261104_232745.json` |
| 검증용 주문 이벤트 | `log/stage0_verify/engine_artifacts/order_event_log_verify.csv` |
| 변경 기록 | `STAGE0_안전장치_변경기록.md` |
