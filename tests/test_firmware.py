"""펌웨어 업데이트 검증.

실제 장치 없이 확인할 수 있는 것은 두 갈래다: UF2를 제대로 걸러내는가,
그리고 부트롬 볼륨의 괴상한 동작(다 쓰고 나면 장치가 사라진다)을 성공으로
다루는가.
"""

import os
import struct
from pathlib import Path

import pytest

from residyn import firmware
from residyn.firmware import FirmwareError

FAMILY = firmware.RP2040_FAMILY_ID


def uf2_block(
    seq: int,
    total: int,
    addr: int = 0x10000000,
    payload: bytes = b"\xaa" * 256,
    *,
    magic0: int = firmware.UF2_MAGIC_START0,
    magic1: int = firmware.UF2_MAGIC_START1,
    end: int = firmware.UF2_MAGIC_END,
    family: int = FAMILY,
    flags: int = firmware.UF2_FLAG_FAMILY_ID,
    payload_size: int | None = None,
) -> bytes:
    head = struct.pack(
        "<8I", magic0, magic1, flags, addr,
        len(payload) if payload_size is None else payload_size,
        seq, total, family,
    )
    body = payload.ljust(firmware.UF2_BLOCK_SIZE - 32 - 4, b"\x00")
    return head + body + struct.pack("<I", end)


def uf2_image(n: int = 4) -> bytes:
    return b"".join(uf2_block(i, n, addr=0x10000000 + 256 * i) for i in range(n))


# ---------------------------------------------------------------------------
# UF2 검증
# ---------------------------------------------------------------------------
def test_accepts_a_well_formed_image():
    info = firmware.inspect_uf2(uf2_image(4))
    assert info.blocks == 4
    assert info.total_bytes == 4 * 256
    assert info.start_address == 0x10000000


def test_bundled_image_is_valid_rp2040_firmware():
    """설치본에 실려 나가는 이미지가 실제로 구울 수 있는 것이어야 한다."""
    data, info = firmware.bundled_uf2()
    assert len(data) % firmware.UF2_BLOCK_SIZE == 0
    assert info.blocks == len(data) // firmware.UF2_BLOCK_SIZE
    # RP2040의 플래시 XIP 시작 주소
    assert info.start_address == 0x10000000


def test_rejects_a_file_that_is_not_block_aligned():
    with pytest.raises(FirmwareError, match="512"):
        firmware.inspect_uf2(uf2_image(2) + b"\x00" * 13)


def test_rejects_an_empty_file():
    with pytest.raises(FirmwareError):
        firmware.inspect_uf2(b"")


def test_rejects_a_non_uf2_file_of_the_right_size():
    """크기만 맞는 아무 파일. 부트롬은 이걸 말없이 버린다."""
    with pytest.raises(FirmwareError, match="매직"):
        firmware.inspect_uf2(b"\x00" * (firmware.UF2_BLOCK_SIZE * 3))


def test_rejects_firmware_for_another_chip():
    """family가 다른 UF2 — 파일은 멀쩡하지만 이 보드용이 아니다."""
    bad = uf2_block(0, 1, family=0x1C5F21B0)   # nRF52840
    with pytest.raises(FirmwareError, match="RP2040"):
        firmware.inspect_uf2(bad)


def test_rejects_a_truncated_image():
    """total_blocks가 실제 블록 수와 다르면 전송이 끊긴 파일이다."""
    full = uf2_image(8)
    with pytest.raises(FirmwareError, match="잘렸"):
        firmware.inspect_uf2(full[: firmware.UF2_BLOCK_SIZE * 5])


def test_rejects_blocks_out_of_order():
    data = uf2_block(0, 2) + uf2_block(0, 2)
    with pytest.raises(FirmwareError, match="순서"):
        firmware.inspect_uf2(data)


def test_rejects_an_oversized_payload_field():
    bad = uf2_block(0, 1, payload_size=500)
    with pytest.raises(FirmwareError, match="payload"):
        firmware.inspect_uf2(bad)


def test_family_id_is_required_before_flashing():
    """flags에 FAMILY_ID가 없으면 그 필드는 family가 아니다."""
    ok = uf2_block(0, 1, flags=0, family=0xDEADBEEF)
    with pytest.raises(FirmwareError):
        firmware.inspect_uf2(ok)


def test_read_uf2_reports_a_missing_file_clearly(tmp_path):
    with pytest.raises(FirmwareError, match="읽지 못"):
        firmware.read_uf2(tmp_path / "nope.uf2")


# ---------------------------------------------------------------------------
# 볼륨 탐색
# ---------------------------------------------------------------------------
def _make_volume(root: Path, text: str = "Board-ID: RPI-RP2\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / firmware.INFO_FILE).write_text(text, encoding="ascii")
    return root


def test_finds_the_volume_by_info_file_not_by_name(tmp_path, monkeypatch):
    """볼륨 이름은 OS·로케일에 따라 다르게 보인다. INFO_UF2.TXT로 찾아야 한다."""
    vol = _make_volume(tmp_path / "NO_NAME")
    monkeypatch.setattr(firmware, "_candidate_roots", lambda: iter([vol]))
    assert firmware.find_bootrom_volume() == vol


def test_ignores_a_volume_that_is_not_a_pico(tmp_path, monkeypatch):
    other = _make_volume(tmp_path / "SOMESTICK", "Board-ID: SOME-OTHER-BOARD\n")
    monkeypatch.setattr(firmware, "_candidate_roots", lambda: iter([other]))
    assert firmware.find_bootrom_volume() is None


def test_unreadable_candidates_are_skipped_not_fatal(tmp_path, monkeypatch):
    """윈도우에서는 빈 드라이브 문자를 전부 훑는다 — 대부분이 에러를 낸다."""
    vol = _make_volume(tmp_path / "RPI-RP2")
    roots = [Path("Z:\\"), tmp_path / "missing", vol]
    monkeypatch.setattr(firmware, "_candidate_roots", lambda: iter(roots))
    assert firmware.find_bootrom_volume() == vol


def test_wait_for_volume_gives_up_and_returns_none(monkeypatch):
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: None)
    assert firmware.wait_for_bootrom_volume(timeout_s=0.05) is None


def test_wait_for_volume_stops_early_when_cancelled(monkeypatch):
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: None)
    assert firmware.wait_for_bootrom_volume(
        timeout_s=30.0, should_cancel=lambda: True
    ) is None


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------
def test_write_puts_the_whole_image_on_the_volume(tmp_path):
    vol = _make_volume(tmp_path / "RPI-RP2")
    data = uf2_image(3)
    firmware.write_uf2_to_volume(vol, data, name="fw.uf2")
    assert (vol / "fw.uf2").read_bytes() == data


def test_device_vanishing_after_the_last_byte_is_success(tmp_path, monkeypatch):
    """부트롬은 마지막 블록을 받자마자 재부팅한다 — close/fsync가 터진다.

    이걸 실패로 보고하면 정상 업데이트가 전부 실패로 보인다.
    """
    vol = _make_volume(tmp_path / "RPI-RP2")

    def boom(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", boom)
    firmware.write_uf2_to_volume(vol, uf2_image(2), name="fw.uf2")   # 예외 없음


def test_failure_before_all_bytes_are_written_is_reported(tmp_path, monkeypatch):
    """쓰기 도중 끊긴 것은 진짜 실패다 — 조용히 넘기면 안 된다."""
    vol = _make_volume(tmp_path / "RPI-RP2")

    class Broken:
        def write(self, _data):
            raise OSError(28, "No space left on device")

        def fileno(self):
            return -1

        def close(self):
            pass

    monkeypatch.setattr("builtins.open", lambda *a, **k: Broken())
    with pytest.raises(FirmwareError, match="중단"):
        firmware.write_uf2_to_volume(vol, uf2_image(2))


def test_a_short_write_resumes_from_where_it_stopped(tmp_path, monkeypatch):
    """버퍼 없는 쓰기는 한 번에 다 나가지 않을 수 있다 — 나머지를 이어 보내야 한다."""
    vol = _make_volume(tmp_path / "RPI-RP2")
    data = uf2_image(3)

    class Partial:
        def __init__(self):
            self.got = bytearray()

        def write(self, chunk):
            n = min(100, len(chunk))     # 매번 조금씩만 받는다
            self.got += chunk[:n]
            return n

        def fileno(self):
            return -1

        def close(self):
            pass

    sink = Partial()
    monkeypatch.setattr("builtins.open", lambda *a, **k: sink)
    firmware.write_uf2_to_volume(vol, data)
    assert bytes(sink.got) == data


def test_a_write_that_stops_making_progress_is_not_an_infinite_loop(
    tmp_path, monkeypatch
):
    class Stalled:
        def write(self, _chunk):
            return 0

        def fileno(self):
            return -1

        def close(self):
            pass

    _make_volume(tmp_path / "RPI-RP2")
    monkeypatch.setattr("builtins.open", lambda *a, **k: Stalled())
    with pytest.raises(FirmwareError, match="멈췄"):
        firmware.write_uf2_to_volume(tmp_path / "RPI-RP2", uf2_image(1))


def test_write_reports_an_unwritable_volume(tmp_path):
    vol = _make_volume(tmp_path / "RPI-RP2")
    with pytest.raises(FirmwareError, match="쓸 수 없"):
        firmware.write_uf2_to_volume(vol / "nonexistent-subdir", uf2_image(1))


# ---------------------------------------------------------------------------
# 전체 절차
# ---------------------------------------------------------------------------
def test_update_validates_before_touching_the_volume(tmp_path, monkeypatch):
    """잘못된 파일로 볼륨을 건드리면 안 된다."""
    touched = []
    monkeypatch.setattr(
        firmware, "find_bootrom_volume", lambda: touched.append("looked") or None
    )
    with pytest.raises(FirmwareError):
        firmware.update_firmware(b"\x00" * 512)
    assert touched == []


def test_update_skips_the_reboot_magic_when_already_in_bootsel(tmp_path, monkeypatch):
    """BOOTSEL을 눌러 꽂은 장치에는 매직을 보낼 포트가 없다."""
    vol = _make_volume(tmp_path / "RPI-RP2")
    data = uf2_image(3)

    def no_magic(_port):
        raise AssertionError("이미 부트로더인데 매직을 보내려 했다")

    monkeypatch.setattr(firmware, "_send_reboot_magic", no_magic)
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: vol)
    monkeypatch.setattr(firmware, "wait_for_device_port", lambda **k: "COM7")

    steps: list[str] = []
    assert firmware.update_firmware(data, port=None, progress=steps.append) == "COM7"
    assert (vol / "firmware.uf2").read_bytes() == data
    assert steps    # 진행 상황이 보고되었다


def test_update_sends_the_magic_when_the_device_is_running_normally(
    tmp_path, monkeypatch
):
    vol = _make_volume(tmp_path / "RPI-RP2")
    sent: list[str] = []

    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: None)
    monkeypatch.setattr(firmware, "bootrom_present", lambda: False)
    monkeypatch.setattr(firmware, "_send_reboot_magic", sent.append)
    monkeypatch.setattr(firmware, "wait_for_bootrom_volume", lambda **k: vol)
    monkeypatch.setattr(firmware, "wait_for_device_port", lambda **k: "COM3")

    assert firmware.update_firmware(uf2_image(2), port="COM3") == "COM3"
    assert sent == ["COM3"]


def test_update_refuses_when_there_is_no_device_at_all(monkeypatch):
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: None)
    monkeypatch.setattr(firmware, "bootrom_present", lambda: False)
    with pytest.raises(FirmwareError, match="BOOTSEL"):
        firmware.update_firmware(uf2_image(1), port=None)


def test_update_explains_when_the_volume_never_appears(monkeypatch):
    """자동 마운트가 꺼져 있으면 여기서 멈춘다 — 무엇을 하라고 말해줘야 한다."""
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: None)
    monkeypatch.setattr(firmware, "bootrom_present", lambda: True)
    monkeypatch.setattr(firmware, "wait_for_bootrom_volume", lambda **k: None)
    with pytest.raises(FirmwareError, match="RPI-RP2"):
        firmware.update_firmware(uf2_image(1), port=None)


def test_flash_succeeds_even_if_the_port_does_not_come_back(tmp_path, monkeypatch):
    """포트 재열거가 늦는 것과 플래시 실패는 다른 일이다."""
    vol = _make_volume(tmp_path / "RPI-RP2")
    monkeypatch.setattr(firmware, "find_bootrom_volume", lambda: vol)
    monkeypatch.setattr(firmware, "wait_for_device_port", lambda **k: None)

    assert firmware.update_firmware(uf2_image(2), port=None) is None
    assert (vol / "firmware.uf2").exists()



@pytest.mark.parametrize('addr',[0x10000100,0x0ffffff0,0x10200000])
def test_rejects_missing_boot_region_or_out_of_range_addresses(addr):
    with pytest.raises(FirmwareError): firmware.inspect_uf2(uf2_block(0,1,addr=addr))
