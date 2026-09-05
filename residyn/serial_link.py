"""USB CDC 링크.

장치는 VID 0x2E8A / PID 0x000A 로 열거된다(RP2040 CDC). 부트로더로 넘어가면
PID 0x0003(PICOBOOT)으로 재열거된다.

포트를 연 뒤 **DTR을 반드시 올린다.** 펌웨어의 pico_stdio_usb는 쓰기 직전마다
stdio_usb_connected()를 확인하는데 그 판정이 DTR에서 나온다. DTR이 내려가
있으면 포트는 정상으로 열리고 리더 스레드도 도는데 수신 바이트만 0이 되는,
원인이 드러나지 않는 실패가 된다. (안드로이드 앱에서 실제로 겪은 문제다.)
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import serial
from serial.tools import list_ports

from .protocol import REBOOT_MAGIC, DataFrame, FrameParser, build_resend_request

RP2040_VID = 0x2E8A
CDC_PID = 0x000A
BOOTROM_PID = 0x0003

_READ_CHUNK = 4096
_READ_TIMEOUT_S = 0.1


@dataclass
class PortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None

    @property
    def is_device(self) -> bool:
        return self.vid == RP2040_VID and self.pid == CDC_PID

    def __str__(self) -> str:
        tag = " (ADXL345)" if self.is_device else ""
        return f"{self.device} — {self.description}{tag}"


def list_serial_ports() -> list[PortInfo]:
    """장치가 먼저 오도록 정렬해서 돌려준다."""
    ports = [
        PortInfo(p.device, p.description or "", p.vid, p.pid)
        for p in list_ports.comports()
    ]
    ports.sort(key=lambda p: (not p.is_device, p.device))
    return ports


def find_device_port() -> str | None:
    for p in list_serial_ports():
        if p.is_device:
            return p.device
    return None


def bootrom_present() -> bool:
    """장치가 PICOBOOT(부트로더)로 열거되어 있는지."""
    return any(
        p.vid == RP2040_VID and p.pid == BOOTROM_PID for p in list_ports.comports()
    )


class SerialLink:
    """백그라운드 스레드로 읽어 파싱된 프레임을 콜백으로 넘긴다.

    콜백은 리더 스레드에서 불린다 — GUI를 직접 만지면 안 되고, 신호로 넘겨야
    한다.
    """

    def __init__(
        self,
        port: str,
        on_frames: Callable[[list[DataFrame]], None],
        on_error: Callable[[str], None] | None = None,
        baudrate: int = 115200,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._on_frames = on_frames
        self._on_error = on_error
        self.parser = FrameParser()

        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    @property
    def measured_odr_hz(self) -> float | None:
        """평균 실측 ODR. 창 하나가 아니라 연결 이후 받은 전부의 평균이다."""
        return self.parser.measured_odr_hz

    @property
    def odr_windows(self) -> int:
        return self.parser.odr_windows

    def open(self) -> None:
        self.parser.reset()
        self._stop.clear()
        self._ser = serial.Serial(
            self.port, self.baudrate, timeout=_READ_TIMEOUT_S
        )
        # 위 주석 참고 — 이걸 빼면 데이터가 한 바이트도 오지 않는다.
        self._ser.dtr = True
        self._ser.rts = True
        self._ser.reset_input_buffer()

        self._thread = threading.Thread(target=self._run, name="residyn-serial", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def send(self, payload: bytes) -> None:
        """명령을 보낸다. 실패해도 예외를 올리지 않는다 — 재전송 요청 하나가
        실패했다고 진행 중인 수신 스트림까지 끊을 이유는 없고, 프로토콜상
        요청이 씹혀도 무방하도록 설계되어 있다."""
        ser = self._ser
        if ser is None or not ser.is_open:
            return
        try:
            with self._write_lock:
                ser.write(payload)
                ser.flush()
        except Exception:
            pass

    def request_resend(self, seq: int) -> None:
        self.send(build_resend_request(seq))

    def enter_bootloader(self) -> None:
        """8바이트 매직을 보내 ROM 부트로더로 재부팅시킨다 (PROTOCOL.md 9절).
        장치가 즉시 재열거되므로 이 링크는 곧 끊긴다."""
        self.send(REBOOT_MAGIC)

    def _run(self) -> None:
        ser = self._ser
        assert ser is not None
        while not self._stop.is_set():
            try:
                chunk = ser.read(_READ_CHUNK)
            except Exception as e:
                if not self._stop.is_set() and self._on_error:
                    self._on_error(f"USB 읽기 오류: {e}")
                return
            if not chunk:
                continue
            try:
                frames = self.parser.feed(chunk)
            except Exception as e:  # 파서 결함이 스레드를 죽이지 않게
                if self._on_error:
                    self._on_error(f"파싱 오류: {e}")
                continue
            if frames:
                self._on_frames(frames)
