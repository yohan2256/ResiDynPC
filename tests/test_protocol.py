"""와이어 프로토콜 파서 검증.

펌웨어는 호스트 연결과 무관하게 계속 전송하므로 파서는 항상 스트림 중간에서
시작한다. 여기서 검증하는 건 "정상 프레임을 읽는다"보다 "깨진 입력에서
빠져나온다" 쪽이다.
"""

import struct

import pytest

from residyn.protocol import (
    FrameParser,
    REBOOT_MAGIC,
    build_resend_request,
    crc16_ccitt,
)


def data_frame(seq: int, samples, flags: int = 0, dropped: int = 0) -> bytes:
    body = struct.pack("<BBHBBH", 0xFA, 0xCE, seq, len(samples), flags, dropped)
    for x, y, z in samples:
        body += struct.pack("<hhh", x, y, z)
    return body + struct.pack("<H", crc16_ccitt(body))


def status_frame(odr_hz: float) -> bytes:
    body = struct.pack("<BBI", 0xFA, 0xCD, int(round(odr_hz * 1000)))
    return body + struct.pack("<H", crc16_ccitt(body))


def test_parses_a_data_frame():
    p = FrameParser()
    frames = p.feed(data_frame(7, [(1, 2, 3), (4, 5, 6)]))
    assert len(frames) == 1
    assert frames[0].seq_start == 7
    assert frames[0].samples == [(1, 2, 3), (4, 5, 6)]


def test_status_frame_sets_measured_odr():
    p = FrameParser()
    p.feed(status_frame(3187.421))
    assert p.measured_odr_hz == pytest.approx(3187.421, abs=1e-3)


def test_frame_split_across_chunks():
    """USB 읽기는 프레임 경계와 무관하게 잘려 들어온다."""
    blob = data_frame(1, [(10, 20, 30)])
    p = FrameParser()
    out = []
    for i in range(len(blob)):
        out += p.feed(blob[i : i + 1])
    assert len(out) == 1
    assert out[0].samples == [(10, 20, 30)]


def test_starts_midstream():
    """포트를 열면 진행 중인 스트림 한가운데로 들어간다."""
    good = data_frame(3, [(1, 1, 1)])
    p = FrameParser()
    frames = p.feed(good[4:] + data_frame(4, [(2, 2, 2)]))
    assert len(frames) == 1
    assert frames[0].seq_start == 4
    assert p.stats.resync_bytes > 0


def test_crc_error_is_skipped_and_stream_recovers():
    bad = bytearray(data_frame(1, [(1, 2, 3)]))
    bad[-1] ^= 0xFF
    p = FrameParser()
    frames = p.feed(bytes(bad) + data_frame(2, [(9, 9, 9)]))
    assert [f.seq_start for f in frames] == [2]
    assert p.stats.crc_errors >= 1


def test_bogus_count_does_not_wedge_the_parser():
    """헤더가 깨져 count가 범위를 벗어나도 다음 프레임을 찾아내야 한다."""
    bogus = struct.pack("<BBHBBH", 0xFA, 0xCE, 0, 200, 0, 0)
    p = FrameParser()
    frames = p.feed(bogus + data_frame(5, [(7, 7, 7)]))
    assert [f.seq_start for f in frames] == [5]


def test_noise_only_input_terminates():
    """0xFA가 전혀 없는 잡음은 버퍼를 비우고 조용히 끝나야 한다 (무한루프 금지)."""
    p = FrameParser()
    assert p.feed(bytes([0x00, 0x11, 0x22] * 500)) == []
    assert p.stats.resync_bytes == 1500


def test_sequence_gap_counted_once():
    p = FrameParser()
    p.feed(data_frame(0, [(0, 0, 0)]))      # 다음 기대값 1
    p.feed(data_frame(50, [(0, 0, 0)]))     # 갭
    assert p.stats.gaps == 1


def test_resend_frame_does_not_count_as_gap():
    """뒤를 채우는 재전송 프레임은 전방 갭이 아니다."""
    p = FrameParser()
    p.feed(data_frame(100, [(0, 0, 0)]))    # 다음 기대값 101
    p.feed(data_frame(90, [(0, 0, 0)]))     # 과거 구간 채움
    assert p.stats.gaps == 0


def test_interleaved_status_and_data():
    p = FrameParser()
    blob = data_frame(1, [(1, 1, 1)]) + status_frame(3200.0) + data_frame(2, [(2, 2, 2)])
    frames = p.feed(blob)
    assert [f.seq_start for f in frames] == [1, 2]
    assert p.measured_odr_hz == pytest.approx(3200.0)


def test_status_frame_third_byte_is_not_read_as_count():
    """0xFA 뒤를 무조건 count로 읽으면 상태 프레임에서 길이 계산이 깨진다."""
    p = FrameParser()
    # odr 하위 바이트가 큰 값이 되도록 고른다
    frames = p.feed(status_frame(4194.303) + data_frame(1, [(5, 5, 5)]))
    assert [f.seq_start for f in frames] == [1]


def test_resend_request_encoding():
    assert build_resend_request(0x1234) == bytes((0xF0, 0x0D, 0x34, 0x12))


def test_reboot_magic_is_eight_bytes():
    # 2바이트로 되돌아가면 회선 잡음이 우연히 부트로더 진입을 트리거할 수 있다.
    assert REBOOT_MAGIC == b"\xf0\xb0REBOOT"
    assert len(REBOOT_MAGIC) == 8


def test_crc_matches_firmware_vector():
    # CRC16-CCITT(0xFFFF init) 표준 벡터
    assert crc16_ccitt(b"123456789") == 0x29B1
