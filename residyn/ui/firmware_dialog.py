"""펌웨어 업데이트 다이얼로그.

업데이트는 초 단위로 끝나지 않고 중간에 장치가 사라졌다 돌아온다. 그래서
작업은 워커 스레드에서 돌리고 진행 상황을 줄 단위로 쌓아 보여준다 — 어느
단계에서 멈췄는지가 보여야 사용자가 BOOTSEL을 눌러야 할지 판단할 수 있다.

측정 중 여부와 무관하게 열 수 있어야 한다. 장치가 이미 BOOTSEL 상태로 꽂혀
있으면 포트가 아예 없는데, 그때야말로 업데이트가 필요한 상황이기 때문이다.
(안드로이드 앱에서 이 버튼을 측정 중에만 활성화해 두었다가 겪은 문제다.)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import firmware
from ..firmware import FirmwareError, Uf2Info


class _Worker(QThread):
    progress = Signal(str)
    done = Signal(str)      # 돌아온 포트 이름 (못 찾았으면 빈 문자열)
    failed = Signal(str)

    def __init__(self, data: bytes, port: str | None, parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._port = port
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            port = firmware.update_firmware(
                self._data,
                port=self._port,
                progress=self.progress.emit,
                should_cancel=lambda: self._cancel,
            )
        except FirmwareError as e:
            self.failed.emit(str(e))
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(f"예상치 못한 오류: {e}")
        else:
            self.done.emit(port or "")


class FirmwareDialog(QDialog):
    def __init__(self, port: str | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("펌웨어 업데이트")
        self.setMinimumWidth(520)

        self._port = port
        self._worker: _Worker | None = None
        self._data: bytes | None = None
        self._info: Uf2Info | None = None
        self._source = "—"

        self.source_label = QLabel()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)

        self.btn_pick = QPushButton("다른 파일 선택...")
        self.btn_pick.clicked.connect(self._pick_file)
        self.btn_run = QPushButton("업데이트 시작")
        self.btn_run.clicked.connect(self._start)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        self.buttons.button(QDialogButtonBox.Close).setText("닫기")
        self.buttons.rejected.connect(self.reject)

        note = QLabel(
            "업데이트 중에는 USB를 뽑지 마세요. 장치가 부트로더로 재부팅했다가"
            " 돌아오므로 포트가 잠시 사라지는 것은 정상입니다."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")

        row = QHBoxLayout()
        row.addWidget(self.btn_pick)
        row.addStretch(1)
        row.addWidget(self.btn_run)

        layout = QVBoxLayout(self)
        layout.addWidget(self.source_label)
        layout.addLayout(row)
        layout.addWidget(self.log)
        layout.addWidget(note)
        layout.addWidget(self.buttons)

        self._load_bundled()

    # ---- 이미지 선택 ---------------------------------------------------------

    def _load_bundled(self) -> None:
        try:
            data, info = firmware.bundled_uf2()
        except FirmwareError as e:
            self._set_image(None, None, "—")
            self._say(str(e))
            self._say("‘다른 파일 선택...’으로 .uf2 파일을 지정하세요.")
            return
        self._set_image(data, info, f"내장 이미지 ({firmware.BUNDLED_UF2.name})")

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "펌웨어 파일 선택", "", "UF2 이미지 (*.uf2);;모든 파일 (*)"
        )
        if not path:
            return
        try:
            data, info = firmware.read_uf2(path)
        except FirmwareError as e:
            self._say(f"사용할 수 없는 파일입니다 — {e}")
            return
        self._set_image(data, info, Path(path).name)
        self._say(f"펌웨어 파일을 바꿨습니다: {path}")

    def _set_image(self, data: bytes | None, info: Uf2Info | None, source: str) -> None:
        self._data = data
        self._info = info
        self._source = source
        if info is None:
            self.source_label.setText("펌웨어: 없음")
        else:
            self.source_label.setText(
                f"펌웨어: {source} — {info.blocks}블록, "
                f"{info.total_bytes:,}바이트 → 0x{info.start_address:08X}"
            )
        self.btn_run.setEnabled(data is not None)

    # ---- 실행 ---------------------------------------------------------------

    def _start(self) -> None:
        if self._data is None or self._worker is not None:
            return

        self.btn_run.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self._say(f"시작 — {self._source}")

        self._worker = _Worker(self._data, self._port, self)
        self._worker.progress.connect(self._say)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, port: str) -> None:
        if port:
            self._say(f"업데이트 완료 — 장치가 {port} 로 돌아왔습니다.")
        else:
            self._say(
                "펌웨어는 기록되었습니다. 다만 측정용 포트를 아직 찾지 못했습니다"
                " — 잠시 뒤 ‘새로고침’을 눌러 확인하세요."
            )
        self._finish()

    def _on_failed(self, msg: str) -> None:
        self._say(f"실패: {msg}")
        self._finish()

    def _finish(self) -> None:
        self._worker = None
        self.btn_run.setEnabled(self._data is not None)
        self.btn_pick.setEnabled(True)

    def _say(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def reject(self) -> None:
        # 워커는 대기 지점에서만 멈춘다. 쓰기 도중이라면 끝까지 두고 기다린다 —
        # 절반만 기록된 플래시로 창을 닫는 것보다 잠깐 기다리는 편이 낫다.
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
            self._worker = None
        super().reject()
