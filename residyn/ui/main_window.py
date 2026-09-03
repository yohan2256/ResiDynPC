"""측정 창.

레이아웃은 앱과 같은 얼개다 — 왼쪽 차트, 오른쪽 제어판. 파형과 스펙트럼을
같이 띄우는데, 공진이 탐색 범위 안에 제대로 들어와 있는지는 스펙트럼을 봐야
알 수 있기 때문이다.

수신은 별도 스레드에서 일어나므로 Qt 시그널로 GUI 스레드에 넘긴다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import rawio, report
from ..analysis import AnalysisConfig, AnalysisResult, analyze
from ..criteria import Grade, convert_48h, grade
from ..protocol import DataFrame
from ..serial_link import SerialLink, list_serial_ports
from .meta_dialog import MetaDialog

RB_CAPACITY = 96_000          # 3200 Hz × 30초
CHART_INTERVAL_MS = 100
CHART_MAX_POINTS = 2000
MIN_SAMPLES = 500

AXES = {"AZ": 2, "AX": 0, "AY": 1}


class MainWindow(QMainWindow):
    _frames_arrived = Signal(list)
    _link_error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ResiDyn — 완충재 동탄성계수 측정")
        self.resize(1280, 760)

        self.link: SerialLink | None = None
        self.buffer = np.zeros(RB_CAPACITY)
        self.count = 0                        # 링 버퍼에 쌓인 총량
        self.result: AnalysisResult | None = None
        self.meta = report.ReportMeta()
        self.loaded_fs: float | None = None    # 파일에서 불러온 경우의 fs

        self._build_ui()

        self._frames_arrived.connect(self._on_frames)
        self._link_error.connect(self._on_link_error)

        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_wave)
        self._chart_timer.setInterval(CHART_INTERVAL_MS)

        self.refresh_ports()

    # ---- UI 구성 ------------------------------------------------------------

    def _build_ui(self) -> None:
        pg.setConfigOptions(antialias=True, background="#1B1B1D", foreground="#D0D0D0")

        self.wave_plot = pg.PlotWidget(title="속도 파형")
        self.wave_plot.setLabel("bottom", "time", units="s")
        self.wave_curve = self.wave_plot.plot(pen=pg.mkPen("#00D5E6", width=1))

        self.spec_plot = pg.PlotWidget(title="주파수 스펙트럼")
        self.spec_plot.setLabel("bottom", "frequency", units="Hz")
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#00D5E6", width=1))
        self.f0_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("#B3261E", width=1))
        self.f0_line.setVisible(False)
        self.spec_plot.addItem(self.f0_line)
        # 탐색 범위를 음영으로 깔아 f0가 범위 안에서 잡혔는지 눈으로 보이게 한다
        self.search_region = pg.LinearRegionItem(
            movable=False, brush=pg.mkBrush(0, 213, 230, 28)
        )
        self.spec_plot.addItem(self.search_region)

        charts = QSplitter(Qt.Vertical)
        charts.addWidget(self.wave_plot)
        charts.addWidget(self.spec_plot)
        charts.setSizes([400, 300])

        root = QSplitter(Qt.Horizontal)
        root.addWidget(charts)
        root.addWidget(self._control_panel())
        root.setSizes([900, 380])
        self.setCentralWidget(root)

        self.status_label = QLabel("준비")
        self.statusBar().addWidget(self.status_label)

    def _control_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # 결과
        res = QGroupBox("분석 결과")
        rl = QVBoxLayout(res)
        self.k_label = QLabel("— MN/m³")
        self.k_label.setStyleSheet("font-size: 26px; font-weight: 800;")
        self.verdict_label = QLabel("")
        self.verdict_label.setStyleSheet("font-size: 15px; font-weight: 700;")
        self.detail_label = QLabel("f₀ : —    손실계수 : —")
        rl.addWidget(QLabel("48시간 환산 (판정값)"))
        rl.addWidget(self.k_label)
        rl.addWidget(self.verdict_label)
        rl.addWidget(self.detail_label)
        layout.addWidget(res)

        # 장치
        dev = QGroupBox("장치")
        dl = QVBoxLayout(dev)
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        btn_refresh = QPushButton("새로고침")
        btn_refresh.clicked.connect(self.refresh_ports)
        row.addWidget(self.port_combo, 1)
        row.addWidget(btn_refresh)
        dl.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_start = QPushButton("측정 시작")
        self.btn_start.clicked.connect(self.start_measure)
        self.btn_stop = QPushButton("정지")
        self.btn_stop.clicked.connect(self.stop_measure)
        self.btn_stop.setEnabled(False)
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
        dl.addLayout(row2)

        self.odr_label = QLabel("실측 ODR : —")
        dl.addWidget(self.odr_label)
        layout.addWidget(dev)

        # 조건
        cond = QGroupBox("입력 조건")
        form = QFormLayout(cond)
        self.mass_spin = self._dspin(8.0, 0.01, 1000.0, 2)
        self.area_spin = self._dspin(0.04, 0.0001, 10.0, 4)
        self.thick_spin = self._dspin(0.03, 0.0001, 1.0, 4)
        self.low_spin = self._dspin(15.0, 0.1, 10000.0, 1)
        self.high_spin = self._dspin(150.0, 0.1, 10000.0, 1)
        self.axis_combo = QComboBox()
        self.axis_combo.addItems(AXES.keys())
        self.method_combo = QComboBox()
        self.method_combo.addItems(["FFT", "PEAK"])
        self.start_cycle_spin = self._ispin(2, 0, 50)
        self.cycles_spin = self._ispin(10, 1, 100)
        form.addRow("하중판 무게 [kg]", self.mass_spin)
        form.addRow("완충재 면적 [m²]", self.area_spin)
        form.addRow("완충재 두께 [m]", self.thick_spin)
        form.addRow("탐색 하한 [Hz]", self.low_spin)
        form.addRow("탐색 상한 [Hz]", self.high_spin)
        form.addRow("센서 축", self.axis_combo)
        form.addRow("해석 방식", self.method_combo)
        form.addRow("시작 주기", self.start_cycle_spin)
        form.addRow("분석 주기 수", self.cycles_spin)
        layout.addWidget(cond)

        # 동작
        act = QGroupBox("분석 · 내보내기")
        al = QVBoxLayout(act)
        self.btn_analyze = QPushButton("분석")
        self.btn_analyze.clicked.connect(self.run_analysis)
        self.btn_report = QPushButton("성적서 저장 (PDF)")
        self.btn_report.clicked.connect(self.save_report)
        self.btn_report.setEnabled(False)
        self.btn_save_raw = QPushButton("원시 데이터 저장")
        self.btn_save_raw.clicked.connect(self.save_raw)
        self.btn_load_raw = QPushButton("원시 데이터 불러오기")
        self.btn_load_raw.clicked.connect(self.load_raw)
        for b in (self.btn_analyze, self.btn_report, self.btn_save_raw, self.btn_load_raw):
            al.addWidget(b)
        layout.addWidget(act)

        layout.addStretch(1)
        return panel

    @staticmethod
    def _dspin(value, lo, hi, decimals) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setValue(value)
        return s

    @staticmethod
    def _ispin(value, lo, hi) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    # ---- 설정 ---------------------------------------------------------------

    def config(self) -> AnalysisConfig:
        return AnalysisConfig(
            area_m2=self.area_spin.value(),
            mass_kg=self.mass_spin.value(),
            search_low_hz=self.low_spin.value(),
            search_high_hz=self.high_spin.value(),
            method=self.method_combo.currentText(),
            start_cycle=self.start_cycle_spin.value(),
            num_cycles=self.cycles_spin.value(),
        )

    @property
    def fs(self) -> float:
        """분석에 쓸 샘플레이트. 실측 ODR을 최우선으로 한다."""
        if self.link is not None and self.link.measured_odr_hz:
            return self.link.measured_odr_hz
        if self.loaded_fs:
            return self.loaded_fs
        return 3200.0

    def samples(self) -> np.ndarray:
        n = min(self.count, RB_CAPACITY)
        if n == 0:
            return np.array([])
        if self.count <= RB_CAPACITY:
            return self.buffer[:n].copy()
        head = self.count % RB_CAPACITY
        return np.concatenate([self.buffer[head:], self.buffer[:head]])

    # ---- 측정 ---------------------------------------------------------------

    def refresh_ports(self) -> None:
        self.port_combo.clear()
        ports = list_serial_ports()
        for p in ports:
            self.port_combo.addItem(str(p), p.device)
        if not ports:
            self.port_combo.addItem("장치 없음", None)
        self._status("장치 목록 갱신됨")

    def start_measure(self) -> None:
        device = self.port_combo.currentData()
        if not device:
            self._status("연결할 포트가 없습니다")
            return

        self.count = 0
        self.loaded_fs = None
        self.link = SerialLink(
            device,
            on_frames=lambda fs: self._frames_arrived.emit(fs),
            on_error=lambda m: self._link_error.emit(m),
        )
        try:
            self.link.open()
        except Exception as e:
            self.link = None
            QMessageBox.critical(self, "연결 실패", f"포트를 열지 못했습니다:\n{e}")
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._chart_timer.start()
        self._status(f"측정 중 — {device}")

    def stop_measure(self) -> None:
        self._chart_timer.stop()
        if self.link is not None:
            self.link.close()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._status(f"정지 — {min(self.count, RB_CAPACITY):,} 샘플")

    def _on_frames(self, frames: list[DataFrame]) -> None:
        idx = AXES[self.axis_combo.currentText()]
        for f in frames:
            for s in f.samples:
                self.buffer[self.count % RB_CAPACITY] = s[idx]
                self.count += 1
        if self.link and self.link.measured_odr_hz:
            self.odr_label.setText(f"실측 ODR : {self.link.measured_odr_hz:.2f} Hz")

    def _on_link_error(self, msg: str) -> None:
        self._status(msg)
        self.stop_measure()

    def _refresh_wave(self) -> None:
        x = self.samples()
        if x.size == 0:
            return
        step = max(1, x.size // CHART_MAX_POINTS)
        y = x[::step]
        t = np.arange(y.size) * step / self.fs
        self.wave_curve.setData(t, y)

    # ---- 분석 · 내보내기 ------------------------------------------------------

    def run_analysis(self) -> None:
        x = self.samples()
        if x.size < MIN_SAMPLES:
            self._status(f"샘플이 부족합니다 ({x.size} < {MIN_SAMPLES})")
            return
        cfg = self.config()
        try:
            self.result = analyze(x, self.fs, cfg)
        except Exception as e:
            QMessageBox.warning(self, "분석 실패", str(e))
            return

        r = self.result
        s48 = convert_48h(r.k_prime_mn_m3)
        g = grade(s48)
        self.k_label.setText(f"{s48:.2f} MN/m³")
        self.verdict_label.setText(f"{g.label}   (측정 {r.k_prime_mn_m3:.3f} × 1.25)")
        self.verdict_label.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: "
            + ("#B3261E" if g is Grade.FAIL else "#00D5E6")
        )
        self.detail_label.setText(f"f₀ : {r.f0_hz:.2f} Hz    손실계수 : {r.eta:.3f}")

        self.wave_curve.setData(r.time_axis, r.vel_time)
        self.spec_curve.setData(r.freq, r.mag)
        self.f0_line.setPos(r.f0_hz)
        self.f0_line.setVisible(True)
        self.search_region.setRegion((cfg.search_low_hz, cfg.search_high_hz))
        self.spec_plot.setXRange(0, min(cfg.search_high_hz * 2, float(r.freq[-1])))

        self.btn_report.setEnabled(True)
        self._status(f"분석 완료 — {x.size:,} 샘플 @ {self.fs:.2f} Hz")

    def save_report(self) -> None:
        if self.result is None:
            return
        dlg = MetaDialog(self.meta, self)
        if not dlg.exec():
            return
        self.meta = dlg.value()

        default = f"ResiDyn_Report_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "성적서 저장", default, "PDF (*.pdf)")
        if not path:
            return

        try:
            report.build(
                path, self.meta, self.config(), self.result,
                thickness_m=self.thick_spin.value(),
                axis=self.axis_combo.currentText(),
                charts=report.ChartData(
                    self.result.time_axis, self.result.vel_time,
                    self.result.freq, self.result.mag,
                ),
            )
        except Exception as e:
            QMessageBox.critical(self, "성적서 생성 실패", str(e))
            return

        if not report.korean_font_available():
            QMessageBox.warning(
                self, "글꼴 경고",
                "한글 글꼴을 찾지 못해 성적서의 한글이 깨질 수 있습니다.\n"
                "나눔고딕 또는 Noto Sans CJK 를 설치한 뒤 다시 저장하세요.",
            )
        self._status(f"성적서 저장됨 — {Path(path).name}")

    def save_raw(self) -> None:
        x = self.samples()
        if x.size == 0:
            self._status("저장할 데이터가 없습니다")
            return
        default = f"ResiDyn_Raw_{datetime.now():%Y%m%d_%H%M%S}{rawio.EXT}"
        path, _ = QFileDialog.getSaveFileName(
            self, "원시 데이터 저장", default, f"ResiDyn raw (*{rawio.EXT});;모든 파일 (*)"
        )
        if not path:
            return
        try:
            rawio.write(path, x, self.fs, axis=self.axis_combo.currentText())
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))
            return
        self._status(f"원시 데이터 {x.size:,} 샘플 저장됨")

    def load_raw(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "원시 데이터 불러오기", "",
            f"ResiDyn raw (*{rawio.EXT} *.wav);;모든 파일 (*)",
        )
        if not path:
            return
        try:
            rec = rawio.read(path)
        except Exception as e:
            QMessageBox.critical(self, "불러오기 실패", str(e))
            return

        n = min(rec.samples.size, RB_CAPACITY)
        self.buffer[:n] = rec.samples[-n:]
        self.count = n
        self.loaded_fs = rec.fs_hz
        if rec.axis in AXES:
            self.axis_combo.setCurrentText(rec.axis)

        note = "주석의 실측값" if rec.fs_from_comment else "헤더값(주석 없음)"
        self.odr_label.setText(f"실측 ODR : {rec.fs_hz:.2f} Hz ({note})")
        self._refresh_wave()
        self._status(f"{n:,} 샘플 불러옴 — {Path(path).name}")

    def _status(self, msg: str) -> None:
        self.status_label.setText(msg)

    def closeEvent(self, event) -> None:   # noqa: N802 (Qt 명명 규칙)
        if self.link is not None:
            self.link.close()
        super().closeEvent(event)
