# 0단계 안전장치 변경 기록 (2026-11-04)

변경 전 백업: `C:\cursorAI_work_backup\pre_stage0_20261104_224113\` (main.py, gui.py, strategy_params.json, git HEAD)

## 신규 파일
| 파일 | 내용 |
|---|---|
| `order_safety.py` | 중앙 주문 게이트(`guarded_send_order`), `OrderSafetyGate`, `SendResult`, `OpenOrder`, `run_emergency_stop`, `InstanceLock`, 안내 문구 |
| `tests/test_order_safety.py` | 44개 테스트 (가짜 API만 사용, 실주문 없음) |
| `STAGE0_안전장치_변경기록.md` | 이 문서 |

## main.py 변경 (전략·매수/매도 조건 변경 없음)
| 위치 | 변경 |
|---|---|
| import | `import order_safety` |
| `KiwoomOpenAPI.__init__` | `server_gubun`, `safety_gate`, `last_holdings_ok_at`, `last_order_msg` 추가 |
| `KiwoomOpenAPI._on_receive_msg` | 주문 TR(rqname "주문"/"취소주문") 서버 메시지를 `[ORDER-MSG]`로 출력 |
| `KiwoomOpenAPI.login` | `self.server_gubun` 저장 |
| `KiwoomOpenAPI.request_holdings` | 응답 성공 시 `last_holdings_ok_at` 기록 |
| `KiwoomOpenAPI.send_order_market` | **SendOrder 직접 호출 제거** → `order_safety.guarded_send_order` 경유, `SendResult` 반환 |
| `KiwoomOpenAPI.cancel_order` (신규) | 미체결 취소 *요청* (같은 게이트 경유) |
| `KiwoomOpenAPI.request_open_orders` (신규) | opt10075 미체결 조회 (**실기 검증 전**) |
| `KiwoomOpenAPI._on_receive_tr_data` | `REQ_OPEN_ORDERS` 파싱 분기 추가 |
| `TradingEngine.__init__` | `_safety_resync_*` 상태 3개 |
| `TradingEngine` 신규 | `_safety_gate`, `_safety_log_rejection`, `_safety_on_tick`, `_safety_schedule_resync`, `_run_safety_resync_deferred`, `_safety_note_open_orders`, `safety_startup_check`, `safety_set_block_buys`, `safety_set_block_all`, `safety_emergency_stop`, `_safety_verify_cancels`, `safety_clear_emergency`, `safety_status_text` |
| `TradingEngine.buy_market` / `sell_market` | 전송 결과 검사. 거부·오류 시 pending/전역매수카운트 상태를 만들지 않고 `False` 반환(정상은 기존과 같이 `None`). ORDER_SENT 이벤트에 "접수요청(체결 미확정)" 메모, pending_orders 에 `api_accepted` |
| `TradingEngine.on_tick` | 연결 상태를 게이트에 반영(`_safety_on_tick`). 매수 3곳은 `False`일 때 break (추가매수 완료 표시 안 함) |
| `TradingEngine.start` | 부팅 로그에 안전 상태, 증권사 미체결 1회 기록 |
| `TradingWindow` | 안전 버튼 3개 + 상태 라벨, `_refresh_safety_widgets`, `_toggle_block_buys`, `_toggle_block_all`, `_emergency_stop`; `_toggle_engine` 정지 시 손절·익절도 멈춘다는 안내; `refresh_ui` 에서 안전 라벨 갱신 |
| `main()` | `InstanceLock` 중복 실행 차단 (실패 시 메시지 후 종료코드 2) |

## 동작 규칙 요약
- 모의서버(`GetServerGubun=="1"`): 기존과 동일하게 주문 허용.
- 실서버: `LIVE_ORDERS_ENABLED=1` 일 때만 허용(기본 0 = 차단). 서버 구분 미확인이면 거부.
- 연결 끊김 감지 즉시 주문 차단 → 복구 후에도 잔고 TR + 미체결 TR 둘 다 성공해야 해제.
- SendOrder 반환 0 = 접수 요청 성공(체결 아님). 체결은 chejan 통보로만 확정.
- 전송 오류 후 같은 종목·방향 재시도 억제(매수 30s / 매도 5s).
- A 신규매수 차단 / B 신규주문 전체 차단(손절 매도도 막힘) / C 긴급정지(매수 차단 + 미체결 취소 요청 + 상태 JSON 기록 `data/safety/`).
- 취소 주문은 A·B·C 상태에서도 허용.

## 미완료 / 한계
1. opt10075(미체결 조회)·취소 주문은 가짜 객체로만 검증됨. 모의서버 실기 확인 필요.
2. 재시작 시 미체결 주문을 `pending_orders` 로 복구하지 않음(조회·기록·경고만).
3. A/B/긴급정지 상태는 앱 재시작 시 초기화됨(영속화 안 함).
4. 정지 버튼의 동작(손절·익절 판단 중단) 자체는 변경하지 않음. 안내 문구만 추가.
5. 증권사에 걸어둔 손절 주문 없음 — 앱 종료·연결 단절 시 자동 손절 보장 없음.
6. 미체결 조회는 첫 페이지만 읽음.
7. 키움 에러코드 표는 문서 기준이며 환경별 차이가 있을 수 있음.
