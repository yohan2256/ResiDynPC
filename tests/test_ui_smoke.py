"""GUI 스모크 테스트 (오프스크린).

픽셀을 확인하는 게 아니라, 창이 실제로 만들어지고 주요 동작이 예외 없이
도는지를 본다. UI 코드는 타이포 하나로도 실행 시점에야 터지는데, 그때는
현장에서 측정 중일 수 있다.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication  # noqa: E402

from residyn import rawio  # noqa: E402
from residyn.criteria import Grade  # noqa: E402
from residyn.protocol import DataFrame  # noqa: E402
from residyn.ui.firmware_dialog import FirmwareDialog  # noqa: E402
from residyn.ui.main_window import RB_CAPACITY, MainWindow  # noqa: E402
from residyn.ui.meta_dialog import MetaDialog  # noqa: E402
from residyn.report import ReportMeta  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    w = MainWindow()
    yield w
    w.close()


def _feed(win, signal, chunk=32):
    """수신 프레임이 도착한 것처럼 밀어 넣는다."""
    for i in range(0, signal.size, chunk):
        block = signal[i : i + chunk]
        win._on_frames([DataFrame(i & 0xFFFF, 0, 0, [(0, 0, int(v)) for v in block])])


def test_window_builds(win):
    assert win.windowTitle().startswith("ResiDyn")
    assert win.samples().size == 0


def test_frames_fill_the_ring_buffer(win):
    _feed(win, np.arange(1000, dtype=float))
    x = win.samples()
    assert x.size == 1000
    assert x[0] == 0 and x[-1] == 999


def test_ring_buffer_keeps_the_newest_when_full(win):
    """용량을 넘기면 오래된 쪽을 버리고 최신 구간을 남겨야 한다."""
    n = RB_CAPACITY + 5000
    _feed(win, np.arange(n, dtype=float) % 1000, chunk=1000)
    x = win.samples()
    assert x.size == RB_CAPACITY
    assert x[-1] == (n - 1) % 1000


def test_analysis_updates_labels_and_charts(win):
    fs = 3200.0
    t = np.arange(6400) / fs
    _feed(win, np.rint(1200 * np.exp(-5 * t) * np.sin(2 * np.pi * 42 * t)), chunk=64)

    win.run_analysis()

    assert win.result is not None
    assert win.result.f0_hz == pytest.approx(42.0, abs=3.0)
    assert "MN/m³" in win.k_label.text()
    assert any(g.label in win.verdict_label.text() for g in Grade)
    assert win.btn_report.isEnabled()
    assert win.f0_line.isVisible()


def test_analysis_refuses_short_capture(win):
    _feed(win, np.zeros(100))
    win.run_analysis()
    assert win.result is None
    assert "부족" in win.status_label.text()


def test_search_range_from_spinboxes_reaches_config(win):
    win.low_spin.setValue(20.0)
    win.high_spin.setValue(120.0)
    cfg = win.config()
    assert (cfg.search_low_hz, cfg.search_high_hz) == (20.0, 120.0)


def test_load_raw_restores_samples_and_exact_rate(win, tmp_path):
    x = np.rint(np.sin(np.arange(4000) / 20.0) * 900)
    p = rawio.write(tmp_path / "t.rdz", x, 3187.4210, axis="AY")

    win.load_raw_path = None
    # 파일 대화상자를 우회해 내부 경로를 직접 태운다
    from PySide6.QtWidgets import QFileDialog

    orig = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(p), ""))
    try:
        win.load_raw()
    finally:
        QFileDialog.getOpenFileName = orig

    assert win.samples().size == x.size
    assert win.fs == pytest.approx(3187.4210, abs=1e-3)
    assert win.axis_combo.currentText() == "AY"


def test_meta_dialog_roundtrips(qapp):
    prev = ReportMeta(site_name="현장", lot_no="L-1", specimen_no=2, specimen_total=3)
    d = MetaDialog(prev)
    v = d.value()
    assert v.site_name == "현장"
    assert (v.specimen_no, v.specimen_total) == (2, 3)


def test_port_refresh_never_leaves_combo_empty(win):
    win.refresh_ports()
    assert win.port_combo.count() >= 1


def test_firmware_button_is_enabled_without_measuring(win):
    """앱에서 이 버튼을 측정 중에만 켜 두었다가 겪은 문제 — 반복하지 않는다.

    BOOTSEL 상태로 꽂힌 장치는 포트가 아예 없는데, 그때가 업데이트가 가장
    필요한 순간이다.
    """
    assert win.btn_firmware.isEnabled()
    win.btn_start.setEnabled(True)      # 측정 전 상태
    assert win.btn_firmware.isEnabled()


def test_firmware_dialog_offers_the_bundled_image(qapp):
    d = FirmwareDialog(port=None)
    try:
        assert "내장 이미지" in d.source_label.text()
        assert d.btn_run.isEnabled()
    finally:
        d.close()


def test_firmware_dialog_rejects_a_bad_file_without_arming_the_button(qapp, tmp_path):
    """엉뚱한 파일을 고르면 이전 이미지가 그대로 남아야 한다 — 조용히 굽지 않는다."""
    from residyn import firmware

    bad = tmp_path / "notfirmware.uf2"
    bad.write_bytes(b"\x00" * 512)

    d = FirmwareDialog(port=None)
    try:
        before = d.source_label.text()
        from PySide6.QtWidgets import QFileDialog

        orig = QFileDialog.getOpenFileName
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(bad), ""))
        try:
            d._pick_file()
        finally:
            QFileDialog.getOpenFileName = orig

        assert d.source_label.text() == before
        assert "사용할 수 없는" in d.log.toPlainText()
        assert d._data == firmware.bundled_uf2()[0]
    finally:
        d.close()



def test_fault_flag_blocks_analysis_and_export(win):
    win._on_frames([DataFrame(0, 1, 0, [(0,0,100)]*600)])
    win.run_analysis()
    assert win.result is None
    assert not win.btn_report.isEnabled()
    assert win.quality_error is not None


def test_axis_change_clears_capture(win):
    _feed(win, np.arange(1000))
    win.axis_combo.setCurrentText("AY")
    assert win.samples().size == 0
    assert win.capture_axis == "AY"


def test_settings_change_invalidates_old_analysis(win):
    t=np.arange(6400)/3200
    _feed(win,np.rint(1000*np.exp(-5*t)*np.sin(2*np.pi*42*t)))
    win.run_analysis()
    assert win.result is not None
    win.mass_spin.setValue(10)
    assert win.result is None
    assert not win.btn_report.isEnabled()


def test_file_rate_has_priority_over_previous_connection(win):
    from types import SimpleNamespace
    win.link=SimpleNamespace(measured_odr_hz=3300.,close=lambda:None)
    win.loaded_fs=3100.
    assert win.fs == 3100.
    win.link=None
