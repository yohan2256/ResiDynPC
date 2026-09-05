"""펌웨어 업데이트.

PC에서는 PICOBOOT 프로토콜을 구현할 필요가 없다. BOOTSEL 상태의 RP2040은
`RPI-RP2` 라는 이름의 USB 대용량 저장장치로 마운트되고, 부트롬이 그 위에
쓰여진 `.uf2` 를 받아 플래시에 굽고 스스로 재부팅한다. 그러니 할 일은
**파일 복사**뿐이다.

안드로이드 앱이 PICOBOOT 클라이언트를 직접 짠 것은 안드로이드가 USB 대용량
저장장치를 마운트해 주지 않아서였다. PC에는 그 제약이 없으므로 pyusb도,
libusb도, 윈도우에서 WinUSB 드라이버를 밀어 넣는 일도 필요 없다.

절차는 네 단계다.

1. 이미 부트로더 상태가 아니면 CDC로 8바이트 매직을 보내 재부팅시킨다
2. `RPI-RP2` 볼륨이 뜰 때까지 기다린다
3. `.uf2` 를 복사한다
4. 측정용 CDC 포트가 돌아올 때까지 기다린다

RP2040은 벽돌이 되지 않는다 — 부트롬은 플래시가 아니라 마스크 ROM에 있어서
BOOTSEL 버튼은 어떤 경우에도 살아 있다. 그래도 3단계 전에 UF2를 검증하는
이유는 안전이 아니라 진단이다: 잘못된 파일은 부트롬이 조용히 무시하고,
사용자에게는 "복사는 됐는데 아무 일도 없음"으로만 보인다.
"""

from __future__ import annotations

import os
import platform
import string
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .serial_link import SerialLink, bootrom_present, find_device_port

# --- UF2 (https://github.com/microsoft/uf2) ---------------------------------
UF2_BLOCK_SIZE = 512
UF2_MAGIC_START0 = 0x0A324655   # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000
RP2040_FAMILY_ID = 0xE48BFF56

# 부트롬이 마운트하는 볼륨의 표식. 이름(RPI-RP2)이 아니라 이 파일의 존재로
# 찾는다 — 볼륨 이름은 OS·로케일에 따라 다르게 보일 수 있다.
INFO_FILE = "INFO_UF2.TXT"
_INFO_MARKERS = ("RP2", "RPI-RP2", "RASPBERRY")

BUNDLED_UF2 = Path(__file__).parent / "data" / "fly_adxl345_firmware.uf2"

REBOOT_SETTLE_S = 15.0    # 매직 전송 후 볼륨이 뜨기까지
RECONNECT_S = 20.0        # 플래시 후 CDC 포트가 돌아오기까지
_POLL_S = 0.3


class FirmwareError(Exception):
    """업데이트를 진행할 수 없는, 사용자에게 그대로 보여줄 만한 실패."""


# ---------------------------------------------------------------------------
# UF2 검증
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Uf2Info:
    blocks: int
    total_bytes: int          # 플래시에 실제로 기록되는 바이트 수
    start_address: int


def inspect_uf2(data: bytes) -> Uf2Info:
    """UF2를 훑어보고 RP2040용으로 쓸 만한지 확인한다.

    부트롬은 잘못된 블록을 말없이 버린다. 여기서 걸러내지 않으면 "복사는
    성공했는데 펌웨어는 그대로"라는, 원인이 드러나지 않는 실패가 된다.
    """
    if not data:
        raise FirmwareError("펌웨어 파일이 비어 있습니다.")
    if len(data) % UF2_BLOCK_SIZE != 0:
        raise FirmwareError(
            f"UF2 형식이 아닙니다 — 크기가 {UF2_BLOCK_SIZE}의 배수여야 하는데 "
            f"{len(data)}바이트입니다."
        )

    n_blocks = len(data) // UF2_BLOCK_SIZE
    payload_total = 0
    start_address: int | None = None

    for i in range(n_blocks):
        block = data[i * UF2_BLOCK_SIZE : (i + 1) * UF2_BLOCK_SIZE]
        magic0, magic1, flags, addr, payload_size, seq, total, family = (
            struct.unpack_from("<8I", block)
        )
        end = struct.unpack_from("<I", block, UF2_BLOCK_SIZE - 4)[0]

        if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1:
            raise FirmwareError(f"UF2 형식이 아닙니다 (블록 {i}: 매직 불일치).")
        if end != UF2_MAGIC_END:
            raise FirmwareError(f"UF2 형식이 아닙니다 (블록 {i}: 끝 매직 불일치).")
        if payload_size > UF2_BLOCK_SIZE - 32 - 4:
            raise FirmwareError(f"UF2 블록 {i}의 payload 크기가 범위를 벗어납니다.")
        if flags & UF2_FLAG_FAMILY_ID and family != RP2040_FAMILY_ID:
            raise FirmwareError(
                "RP2040용 펌웨어가 아닙니다 "
                f"(family 0x{family:08X}, 기대값 0x{RP2040_FAMILY_ID:08X})."
            )
        if total != n_blocks:
            raise FirmwareError(
                f"UF2가 잘렸습니다 — {total}블록이라고 적혀 있는데 {n_blocks}개뿐입니다."
            )
        if seq != i:
            raise FirmwareError(f"UF2 블록 순서가 어긋납니다 ({i}번 자리에 {seq}번).")

        if start_address is None:
            start_address = addr
        payload_total += payload_size

    assert start_address is not None
    return Uf2Info(n_blocks, payload_total, start_address)


def read_uf2(path: str | os.PathLike[str]) -> tuple[bytes, Uf2Info]:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise FirmwareError(f"펌웨어 파일을 읽지 못했습니다: {e}") from e
    return data, inspect_uf2(data)


def bundled_uf2() -> tuple[bytes, Uf2Info]:
    """패키지에 함께 배포되는 펌웨어 이미지."""
    if not BUNDLED_UF2.is_file():
        raise FirmwareError(
            "내장 펌웨어 이미지가 없습니다. 설치가 온전한지 확인하거나 "
            "파일을 직접 골라 주세요."
        )
    return read_uf2(BUNDLED_UF2)


# ---------------------------------------------------------------------------
# RPI-RP2 볼륨 찾기
# ---------------------------------------------------------------------------
def _candidate_roots() -> Iterator[Path]:
    """OS별로 이동식 볼륨이 붙을 만한 자리."""
    system = platform.system()

    if system == "Windows":
        for letter in string.ascii_uppercase:
            yield Path(f"{letter}:\\")
        return

    if system == "Darwin":
        yield from _children("/Volumes")
        return

    # Linux: 자동 마운트 위치를 먼저 보고, 그다음 실제 마운트 테이블을 훑는다.
    for base in ("/media", "/run/media", "/mnt"):
        for entry in _children(base):
            yield entry
            # /media/<user>/RPI-RP2 처럼 한 겹 더 들어가는 배치
            yield from _children(entry)

    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in ("vfat", "msdos", "fuseblk"):
                    yield Path(parts[1].replace("\\040", " "))
    except OSError:
        pass


def _children(base: str | Path) -> Iterator[Path]:
    try:
        yield from sorted(Path(base).iterdir())
    except OSError:
        return


def _looks_like_bootrom(root: Path) -> bool:
    info = root / INFO_FILE
    try:
        if not info.is_file():
            return False
        text = info.read_text(encoding="ascii", errors="replace").upper()
    except OSError:
        # 빈 드라이브 문자, 권한 없음, 준비되지 않은 리더 — 후보가 아닐 뿐이다.
        return False
    return any(m in text for m in _INFO_MARKERS)


def find_bootrom_volume() -> Path | None:
    """마운트된 RPI-RP2 볼륨. 없으면 None."""
    seen: set[str] = set()
    for root in _candidate_roots():
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_bootrom(root):
            return root
    return None


def wait_for_bootrom_volume(
    timeout_s: float = REBOOT_SETTLE_S,
    should_cancel: Callable[[], bool] | None = None,
) -> Path | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            return None
        vol = find_bootrom_volume()
        if vol is not None:
            # 부트롬이 볼륨을 올린 직후에는 쓰기가 이르게 실패할 수 있다.
            time.sleep(0.4)
            return vol
        time.sleep(_POLL_S)
    return None


def wait_for_device_port(
    timeout_s: float = RECONNECT_S,
    should_cancel: Callable[[], bool] | None = None,
) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            return None
        port = find_device_port()
        if port is not None:
            return port
        time.sleep(_POLL_S)
    return None


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------
def write_uf2_to_volume(volume: Path, data: bytes, name: str = "firmware.uf2") -> None:
    """UF2를 부트롬 볼륨에 쓴다.

    마지막 블록을 받는 순간 부트롬이 곧바로 재부팅하므로 flush/fsync/close가
    장치 소멸로 실패할 수 있다. **그건 성공이다.** 바이트를 다 넘긴 뒤에 나온
    OSError는 삼킨다 — 여기서 예외를 올리면 정상 업데이트가 실패로 보고된다.

    그래서 **버퍼 없이** 연다. 버퍼가 끼면 write()가 성공해도 바이트가 아직
    장치에 가지 않은 상태일 수 있고, 그러면 뒤이은 flush 실패가 "다 보낸 뒤의
    재부팅"인지 "덜 보내고 끊긴 것"인지 구분되지 않는다. 버퍼가 없으면
    write()가 곧 전송이라 그 구분이 성립한다.
    """
    target = volume / name
    try:
        f = open(target, "wb", buffering=0)
    except OSError as e:
        raise FirmwareError(f"부트로더 볼륨에 쓸 수 없습니다: {e}") from e

    try:
        sent = 0
        while sent < len(data):
            try:
                n = f.write(data[sent:])
            except OSError as e:
                # 다 쓰기 전에 끊긴 것은 진짜 실패다.
                raise FirmwareError(
                    f"펌웨어 전송이 중단되었습니다 ({sent:,}/{len(data):,}바이트): {e}"
                ) from e
            if not n:
                raise FirmwareError(
                    f"펌웨어 전송이 멈췄습니다 ({sent:,}/{len(data):,}바이트)."
                )
            sent += n

        try:
            os.fsync(f.fileno())
        except (OSError, ValueError):
            pass   # 재부팅해 사라진 것 — 정상 (fd가 이미 무효면 ValueError)
    finally:
        try:
            f.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 전체 절차
# ---------------------------------------------------------------------------
def update_firmware(
    data: bytes,
    *,
    port: str | None = None,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str | None:
    """펌웨어를 굽고, 돌아온 CDC 포트 이름을 돌려준다.

    `port` 는 매직을 보낼 측정용 포트다. 장치가 이미 부트로더 상태라면
    필요 없다 — BOOTSEL을 눌러 꽂은 경우가 그렇다.

    돌려주는 값이 None이면 플래시 자체는 끝났으나 포트를 다시 찾지 못한
    것이다(호스트가 재열거에 시간이 더 걸리는 경우). 실패가 아니다.
    """
    info = inspect_uf2(data)   # 볼륨을 건드리기 전에 검증한다

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    def cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    volume = find_bootrom_volume()

    if volume is None:
        if not bootrom_present():
            if port is None:
                raise FirmwareError(
                    "장치를 찾지 못했습니다. 연결을 확인하거나, BOOTSEL을 누른 채 "
                    "USB를 꽂아 부트로더로 진입시킨 뒤 다시 시도하세요."
                )
            say("부트로더 진입 요청 중...")
            _send_reboot_magic(port)

        say("부트로더 볼륨을 기다리는 중...")
        volume = wait_for_bootrom_volume(should_cancel=should_cancel)

    if cancelled():
        return None
    if volume is None:
        raise FirmwareError(
            "부트로더 볼륨(RPI-RP2)이 나타나지 않았습니다. 자동 마운트가 꺼져 "
            "있을 수 있습니다 — BOOTSEL을 누른 채 USB를 다시 꽂아 보세요."
        )

    say(f"{volume} 에 쓰는 중... ({info.blocks}블록, {info.total_bytes:,}바이트)")
    write_uf2_to_volume(volume, data)

    say("재부팅을 기다리는 중...")
    found = wait_for_device_port(should_cancel=should_cancel)
    say("완료" if found else "플래시 완료 — 포트 재연결을 확인하세요")
    return found


def _send_reboot_magic(port: str) -> None:
    link = SerialLink(port, on_frames=lambda _f: None, on_error=lambda _m: None)
    try:
        link.open()
        link.enter_bootloader()
    except Exception as e:
        raise FirmwareError(f"부트로더 진입 요청을 보내지 못했습니다: {e}") from e
    finally:
        link.close()


__all__ = [
    "BUNDLED_UF2",
    "RP2040_FAMILY_ID",
    "FirmwareError",
    "Uf2Info",
    "bundled_uf2",
    "find_bootrom_volume",
    "inspect_uf2",
    "read_uf2",
    "update_firmware",
    "wait_for_bootrom_volume",
    "wait_for_device_port",
    "write_uf2_to_volume",
]
