# -*- coding: utf-8 -*-
"""
gui.py — 수익률 분석 대시보드 (PyQt5 + matplotlib)

- 실시간 요약(총 수익률, 보유, 거래 로그)
- 시간별 누적 수익률 / 누적 실현손익 그래프
- 주기적 자동 갱신 (기본 5초)
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QDate, Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import matplotlib
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from performance import (
    TradeRecord,
    TradeTracker,
    distinct_codes,
    filter_trade_records,
    summarize_for_dashboard_filtered,
    summarize_records,
    _safe_float,
)

# Windows 한글 깨짐/글리프 경고 방지 (폰트 없는 환경은 무시)
try:
    matplotlib.rcParams["font.family"] = "Malgun Gothic"
    matplotlib.rcParams["axes.unicode_minus"] = False
except Exception:
    pass


# 대시보드 자동 갱신 주기(초). 1~5 권장.
DASHBOARD_REFRESH_SEC = 5


class MplCanvas(FigureCanvas):
    """matplotlib Figure를 Qt 위젯으로 감싼다."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        self.fig = Figure(figsize=(5, 3.5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        super().__init__(self.fig)
        self.setParent(parent)
        self.fig.tight_layout()


class PerformanceDashboard(QMainWindow):
    """
    수익률 분석 GUI 대시보드.

    - engine: TradingEngine (현재가·보유)
    - tracker: TradeTracker (저장된 거래·집계)
    """

    def __init__(
        self,
        engine: Optional[Any] = None,
        tracker: Optional[TradeTracker] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.tracker = tracker or TradeTracker()
        self._name_cache: Dict[str, str] = {}
        self.setWindowTitle("수익률 분석 대시보드")
        self.resize(1100, 820)

        self.setStyleSheet(
            """
            QMainWindow { background: #f0f4f8; }
            QLabel#bigReturn { font-size: 22px; font-weight: 700; color: #0f172a; }
            QLabel.card { background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 12px; }
            QTableWidget { background: #fff; gridline-color: #e2e8f0; font-size: 11px; }
            QHeaderView::section { background: #e0e7ff; font-weight: 700; padding: 4px; }
            QPushButton { background: #3b82f6; color: white; border: none; border-radius: 6px; padding: 6px 12px; }
            """
        )

        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)

        # 상단: 총 수익률 + 요약 카드
        top = QHBoxLayout()
        self.lbl_total = QLabel("전체 수익률(실현 기준): —")
        self.lbl_total.setObjectName("bigReturn")
        self.lbl_pnl = QLabel("실현손익: — | 매수누적: — | 거래건수: —")
        self.lbl_pnl.setProperty("class", "card")
        self.lbl_pnl.setStyleSheet("background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:8px 12px;")
        self.lbl_metrics = QLabel("승률: — | 평균수익: — | 평균손실: — | 손익비: — | MDD: —")
        self.lbl_metrics.setProperty("class", "card")
        self.lbl_metrics.setStyleSheet(
            "background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:8px 12px;"
        )
        top.addWidget(self.lbl_total)
        top.addStretch(1)
        top.addWidget(self.lbl_pnl)
        top.addWidget(self.lbl_metrics)
        btn_reload = QPushButton("CSV 다시읽기")
        btn_reload.clicked.connect(self._reload_csv)
        top.addWidget(btn_reload)
        main.addLayout(top)

        # 필터: 종목 / 날짜 / 조회 / 자동 새로고침
        filt = QHBoxLayout()
        filt.addWidget(QLabel("종목:"))
        self.combo_code = QComboBox()
        self.combo_code.setMinimumWidth(140)
        self.combo_code.addItem("전체", "")
        filt.addWidget(self.combo_code)

        filt.addWidget(QLabel("시작일:"))
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setDate(QDate.currentDate().addDays(-30))
        filt.addWidget(self.date_from)

        filt.addWidget(QLabel("종료일:"))
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setDate(QDate.currentDate())
        filt.addWidget(self.date_to)

        self.chk_all_period = QCheckBox("전체 기간")
        self.chk_all_period.setChecked(True)
        self.chk_all_period.toggled.connect(self._on_all_period_toggled)
        filt.addWidget(self.chk_all_period)

        btn_apply = QPushButton("조회")
        btn_apply.clicked.connect(self.refresh)
        filt.addWidget(btn_apply)

        self.btn_auto_refresh = QPushButton("자동 새로고침: ON")
        self.btn_auto_refresh.setCheckable(True)
        self.btn_auto_refresh.setChecked(True)
        self.btn_auto_refresh.toggled.connect(self._on_auto_refresh_toggled)
        filt.addWidget(self.btn_auto_refresh)

        filt.addStretch(1)
        self.lbl_filter_hint = QLabel("필터: 전체 기간 · 전체 종목")
        self.lbl_filter_hint.setStyleSheet("color:#475569;font-size:11px;")
        filt.addWidget(self.lbl_filter_hint)
        main.addLayout(filt)

        self._on_all_period_toggled(self.chk_all_period.isChecked())

        splitter = QSplitter(Qt.Horizontal)

        # 좌: 보유 / 종목별 수익
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("<b>보유 종목</b>"))
        self.tbl_holdings = QTableWidget(0, 5)
        self.tbl_holdings.setHorizontalHeaderLabels(["업체명", "수량", "평단", "현재가", "평가수익률(%)"])
        lv.addWidget(self.tbl_holdings)

        lv.addWidget(QLabel("<b>종목별 실현 수익률(%)</b>"))
        self.tbl_symbols = QTableWidget(0, 2)
        self.tbl_symbols.setHorizontalHeaderLabels(["종목", "실현수익률(%)"])
        lv.addWidget(self.tbl_symbols)

        splitter.addWidget(left)

        # 우: 탭(거래 로그 / 일별)
        tabs = QTabWidget()
        self.tbl_trades = QTableWidget(0, 7)
        self.tbl_trades.setHorizontalHeaderLabels(
            ["시간", "업체명", "구분", "가격", "수량", "수익률(%)", "실현(원)"]
        )
        tabs.addTab(self.tbl_trades, "거래 로그")

        self.tbl_daily = QTableWidget(0, 2)
        self.tbl_daily.setHorizontalHeaderLabels(["일자", "일별수익률(%)"])
        tabs.addTab(self.tbl_daily, "일별 수익률")

        splitter.addWidget(tabs)
        splitter.setSizes([420, 520])

        main.addWidget(splitter)

        # 그래프 영역 (세로 2개)
        graph_row = QHBoxLayout()
        self.canvas_ret = MplCanvas()
        self.canvas_cum = MplCanvas()
        graph_row.addWidget(self.canvas_ret)
        graph_row.addWidget(self.canvas_cum)
        main.addLayout(graph_row)

        self._timer = QTimer(self)
        self._timer.setInterval(DASHBOARD_REFRESH_SEC * 1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    @staticmethod
    def _qdate_to_date(qd: QDate) -> date:
        return date(qd.year(), qd.month(), qd.day())

    def _filter_params(self) -> Tuple[Optional[str], Optional[date], Optional[date]]:
        """현재 UI에서 종목코드·기간 추출 (전체 기간이면 날짜 None)."""
        raw = self.combo_code.currentData()
        code: Optional[str] = None
        if raw is not None and str(raw).strip():
            code = str(raw).strip()

        if self.chk_all_period.isChecked():
            return code, None, None

        df = self._qdate_to_date(self.date_from.date())
        dt = self._qdate_to_date(self.date_to.date())
        if df > dt:
            df, dt = dt, df
        return code, df, dt

    def _display_name(self, code: str) -> str:
        code_s = str(code).strip()
        if not code_s:
            return ""
        cached = self._name_cache.get(code_s)
        if cached:
            return cached
        name = code_s
        try:
            parent = self.parent()
            code_names = getattr(parent, "code_names", {}) or {}
            name = str(code_names.get(code_s) or code_s)
        except Exception:
            name = code_s
        if name == code_s and self.engine is not None:
            try:
                kw = getattr(self.engine, "kiwoom", None)
                if kw is not None:
                    fetched = str(kw.get_master_code_name(code_s)).strip()
                    if fetched:
                        name = fetched
            except Exception:
                pass
        self._name_cache[code_s] = name
        return name

    def _filtered_summary(self) -> Dict[str, Any]:
        if self.tracker is None:
            return summarize_records([])
        c, df, dt = self._filter_params()
        return summarize_for_dashboard_filtered(self.tracker, code=c, date_from=df, date_to=dt)

    def _filtered_trade_records(self) -> List[TradeRecord]:
        if self.tracker is None:
            return []
        c, df, dt = self._filter_params()
        return filter_trade_records(self.tracker.all_records(), code=c, date_from=df, date_to=dt)

    def _sync_symbol_combo(self) -> None:
        """로그에 나온 종목으로 콤보 갱신 (선택 유지)."""
        if self.tracker is None:
            return
        try:
            codes = distinct_codes(self.tracker.all_records())
        except Exception:
            codes = []
        prev = self.combo_code.currentData()
        self.combo_code.blockSignals(True)
        self.combo_code.clear()
        self.combo_code.addItem("전체", "")
        for x in codes:
            self.combo_code.addItem(self._display_name(x), x)
        idx = self.combo_code.findData(prev)
        if idx >= 0:
            self.combo_code.setCurrentIndex(idx)
        else:
            self.combo_code.setCurrentIndex(0)
        self.combo_code.blockSignals(False)

    def _update_filter_hint(self) -> None:
        c, df, dt = self._filter_params()
        parts: List[str] = []
        if c:
            parts.append(f"종목 {self._display_name(c)}")
        else:
            parts.append("전체 종목")
        if df is None and dt is None:
            parts.append("전체 기간")
        else:
            parts.append(f"{df} ~ {dt}")
        self.lbl_filter_hint.setText("필터: " + " · ".join(parts))

    def _on_all_period_toggled(self, checked: bool) -> None:
        """전체 기간이면 날짜 편집 비활성."""
        self.date_from.setEnabled(not checked)
        self.date_to.setEnabled(not checked)

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        if checked:
            self._timer.start()
            self.btn_auto_refresh.setText("자동 새로고침: ON")
        else:
            self._timer.stop()
            self.btn_auto_refresh.setText("자동 새로고침: OFF")

    def set_engine(self, engine: Optional[Any]) -> None:
        self.engine = engine

    def set_tracker(self, tracker: TradeTracker) -> None:
        self.tracker = tracker

    def _reload_csv(self) -> None:
        if self.tracker is None:
            return
        try:
            self.tracker.load()
        except Exception:
            pass
        self.refresh()

    def refresh(self) -> None:
        """테이블·라벨·차트 전부 갱신. 데이터 없을 때 예외 없이 처리."""
        try:
            self._sync_symbol_combo()
            self._update_filter_hint()
            self._refresh_summary()
            self._refresh_holdings()
            self._refresh_trade_tables()
            self._refresh_charts()
        except Exception:
            pass

    def _refresh_summary(self) -> None:
        summ = self._filtered_summary()
        overall = _safe_float(summ.get("overall_return_pct"), 0.0)
        cost = _safe_float(summ.get("total_buy_cost_krw"), 0.0)
        realized_cost = _safe_float(summ.get("realized_cost_krw"), 0.0)
        pnl = _safe_float(summ.get("total_realized_pnl_krw"), 0.0)
        n = int(summ.get("trade_count") or 0)
        win_rate = _safe_float(summ.get("win_rate_pct"), 0.0)
        avg_profit = _safe_float(summ.get("avg_profit"), 0.0)
        avg_loss = _safe_float(summ.get("avg_loss"), 0.0)
        pl_ratio = _safe_float(summ.get("profit_loss_ratio"), 0.0)
        mdd = _safe_float(summ.get("mdd_pct"), 0.0)
        color = "#16a34a" if overall >= 0 else "#dc2626"
        self.lbl_total.setText(f"수익률(필터·실현 기준): {overall:+.2f}%")
        self.lbl_total.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {color};")
        self.lbl_pnl.setText(
            f"실현손익: {pnl:,.0f}원 | 실현원금(근사): {realized_cost:,.0f}원 "
            f"| 매수누적: {cost:,.0f}원 | 기록건수: {n}"
        )
        self.lbl_metrics.setText(
            "승률: "
            f"{win_rate:.2f}% | 평균수익: {avg_profit:,.0f}원 | 평균손실: {avg_loss:,.0f}원 "
            f"| 손익비: {pl_ratio:.2f} | MDD: {mdd:.2f}%"
        )

    def _refresh_holdings(self) -> None:
        self.tbl_holdings.setRowCount(0)
        if self.engine is None:
            return
        try:
            positions = getattr(self.engine, "positions", {}) or {}
            prices = getattr(self.engine, "current_price", {}) or {}
        except Exception:
            return

        code_filter, _, _ = self._filter_params()

        rows: List[tuple] = []
        for code, pos in positions.items():
            if code_filter and str(code).strip() != code_filter:
                continue
            qty = int(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue
            avg = float(pos.get("avg_price", 0) or 0)
            cur = int(prices.get(code, 0) or 0)
            if avg > 0 and cur > 0:
                ur = (cur - avg) / avg * 100.0
                if not math.isfinite(ur):
                    ur = 0.0
            else:
                ur = 0.0
            rows.append((self._display_name(str(code)), qty, avg, cur, ur))

        self.tbl_holdings.setRowCount(len(rows))
        for i, (name, qty, avg, cur, ur) in enumerate(rows):
            vals = [
                name,
                str(qty),
                f"{avg:,.0f}",
                f"{cur:,}" if cur else "-",
                f"{ur:+.2f}",
            ]
            for j, v in enumerate(vals):
                self.tbl_holdings.setItem(i, j, QTableWidgetItem(str(v)))

        summ = self._filtered_summary()
        sym_ret: Dict[str, float] = summ.get("symbol_return_pct") or {}
        self.tbl_symbols.setRowCount(len(sym_ret))
        for i, (code, pct) in enumerate(sorted(sym_ret.items(), key=lambda x: x[0])):
            self.tbl_symbols.setItem(i, 0, QTableWidgetItem(self._display_name(code)))
            self.tbl_symbols.setItem(i, 1, QTableWidgetItem(f"{_safe_float(pct, 0.0):+.2f}"))

    def _refresh_trade_tables(self) -> None:
        recs = self._filtered_trade_records()
        self.tbl_trades.setRowCount(len(recs))
        for i, r in enumerate(sorted(recs, key=lambda x: x.ts, reverse=True)):
            ts_s = r.ts.strftime("%m-%d %H:%M:%S")
            pnl_s = "" if r.pnl_pct is None else f"{r.pnl_pct:+.3f}"
            self.tbl_trades.setItem(i, 0, QTableWidgetItem(ts_s))
            self.tbl_trades.setItem(i, 1, QTableWidgetItem(self._display_name(r.code)))
            self.tbl_trades.setItem(i, 2, QTableWidgetItem(r.side))
            self.tbl_trades.setItem(i, 3, QTableWidgetItem(f"{r.price:,.2f}"))
            self.tbl_trades.setItem(i, 4, QTableWidgetItem(str(r.qty)))
            self.tbl_trades.setItem(i, 5, QTableWidgetItem(pnl_s))
            self.tbl_trades.setItem(i, 6, QTableWidgetItem(f"{r.realized_pnl_krw:,.0f}"))

        summ = self._filtered_summary()
        daily = summ.get("daily_return_pct") or {}
        self.tbl_daily.setRowCount(len(daily))
        for i, (d, pct) in enumerate(sorted(daily.items(), reverse=True)):
            self.tbl_daily.setItem(i, 0, QTableWidgetItem(str(d)))
            self.tbl_daily.setItem(i, 1, QTableWidgetItem(f"{_safe_float(pct, 0.0):+.4f}"))

    def _refresh_charts(self) -> None:
        summ = self._filtered_summary()
        series_ret = summ.get("cum_return_pct_series") or []
        series_cum = summ.get("cum_pnl_series") or []

        # --- 누적 수익률 ---
        ax1 = self.canvas_ret.ax
        ax1.clear()
        ax1.set_title("시간별 누적 수익률(%) — 필터 적용", fontsize=11)
        ax1.set_xlabel("시간")
        ax1.grid(True, alpha=0.3)
        if len(series_ret) < 1:
            ax1.text(0.5, 0.5, "데이터 없음", ha="center", va="center", transform=ax1.transAxes)
        else:
            xs = [t[0] for t in series_ret]
            ys = [_safe_float(t[1], 0.0) for t in series_ret]
            ax1.plot(xs, ys, color="#2563eb", linewidth=1.5)
            self.canvas_ret.fig.autofmt_xdate()
        self.canvas_ret.draw()

        # --- 누적 실현손익(원) ---
        ax2 = self.canvas_cum.ax
        ax2.clear()
        ax2.set_title("거래 누적 실현손익(원) — 필터 적용", fontsize=11)
        ax2.set_xlabel("시간")
        ax2.grid(True, alpha=0.3)
        if len(series_cum) < 1:
            ax2.text(0.5, 0.5, "데이터 없음", ha="center", va="center", transform=ax2.transAxes)
        else:
            xs = [t[0] for t in series_cum]
            ys = [_safe_float(t[1], 0.0) for t in series_cum]
            ax2.plot(xs, ys, color="#059669", linewidth=1.5)
            self.canvas_cum.fig.autofmt_xdate()
        self.canvas_cum.draw()


def open_dashboard(
    engine: Optional[Any],
    tracker: TradeTracker,
    parent: Optional[QWidget] = None,
) -> PerformanceDashboard:
    """대시보드 창을 생성해 표시."""
    w = PerformanceDashboard(engine=engine, tracker=tracker, parent=parent)
    w.show()
    return w
