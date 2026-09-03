"""판정 기준과 원시 파일 입출력 검증."""

import struct
import wave

import numpy as np
import pytest

from residyn import rawio
from residyn.criteria import CORRECTION_48H, Grade, convert_48h, grade


# ---- 판정 -------------------------------------------------------------------

def test_48h_conversion():
    assert convert_48h(13.273) == pytest.approx(16.59, abs=0.01)


def test_grade_uses_converted_value_not_measured():
    """측정값 13.27은 '우수' 구간이지만 환산하면 '합격'이다."""
    measured = 13.273
    assert grade(measured) is Grade.EXCELLENT
    assert grade(convert_48h(measured)) is Grade.PASS


@pytest.mark.parametrize(
    "value,expected",
    [
        (15.0, Grade.EXCELLENT),
        (15.0001, Grade.PASS),
        (20.0, Grade.PASS),
        (20.0001, Grade.FAIL),
    ],
)
def test_grade_boundaries_are_inclusive(value, expected):
    assert grade(value) is expected


def test_correction_factor_is_documented_value():
    assert CORRECTION_48H == 1.25


# ---- .rdz 입출력 ------------------------------------------------------------

def test_roundtrip_preserves_samples(tmp_path):
    x = np.arange(-1000, 1000, dtype=float)
    p = rawio.write(tmp_path / "a.rdz", x, 3187.4210, axis="AZ")
    rec = rawio.read(p)
    assert np.array_equal(rec.samples, x)
    assert rec.axis == "AZ"


def test_exact_sample_rate_survives_header_rounding(tmp_path):
    """헤더는 정수만 담으므로 정확한 ODR은 주석에서 복원해야 한다."""
    p = rawio.write(tmp_path / "b.rdz", np.zeros(64), 3187.4210)
    with wave.open(str(p), "rb") as w:
        assert w.getframerate() == 3187          # 헤더는 반올림된 값

    rec = rawio.read(p)
    assert rec.fs_from_comment is True
    assert rec.fs_hz == pytest.approx(3187.4210, abs=1e-4)


def test_falls_back_to_header_rate_without_comment(tmp_path):
    """주석 없는 평범한 WAV도 읽혀야 한다 (외부 도구로 만든 파일)."""
    p = tmp_path / "plain.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(3200)
        w.writeframes(np.zeros(32, dtype="<i2").tobytes())

    rec = rawio.read(p)
    assert rec.fs_from_comment is False
    assert rec.fs_hz == 3200.0


def test_written_file_is_a_valid_wav(tmp_path):
    """확장자만 바꾼 것이므로 표준 wave 모듈이 그대로 읽어야 한다."""
    x = np.arange(999, dtype=float) % 500 - 250      # 홀수 길이
    p = rawio.write(tmp_path / "c.rdz", x, 3200.0)
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == x.size


def test_riff_size_field_matches_file(tmp_path):
    """INFO 청크를 덧붙인 뒤 RIFF 크기를 갱신하지 않으면 파일이 깨진다."""
    p = rawio.write(tmp_path / "d.rdz", np.zeros(999), 3200.0, extra="odd length")
    blob = p.read_bytes()
    assert struct.unpack_from("<I", blob, 4)[0] == len(blob) - 8


def test_chunk_chain_stays_aligned(tmp_path):
    """홀수 길이 payload·주석에서 패딩을 빠뜨리면 뒤 청크 오프셋이 밀린다."""
    p = rawio.write(tmp_path / "e.rdz", np.zeros(999), 3200.0, extra="x")
    blob = p.read_bytes()
    seen, pos = [], 12
    while pos + 8 <= len(blob):
        cid = blob[pos : pos + 4]
        size = struct.unpack_from("<I", blob, pos + 4)[0]
        seen.append(cid)
        pos += 8 + size + (size % 2)
    assert pos == len(blob), "청크 체인이 파일 끝과 맞지 않는다"
    assert b"data" in seen and b"LIST" in seen


def test_clipping_is_defensive_only(tmp_path):
    """정상 경로(int16 범위)에서는 손실이 없어야 한다."""
    x = np.array([-32768.0, -1.0, 0.0, 1.0, 32767.0])
    rec = rawio.read(rawio.write(tmp_path / "f.rdz", x, 3200.0))
    assert np.array_equal(rec.samples, x)


def test_empty_and_bad_rate_rejected(tmp_path):
    with pytest.raises(ValueError):
        rawio.write(tmp_path / "g.rdz", np.array([]), 3200.0)
    with pytest.raises(ValueError):
        rawio.write(tmp_path / "h.rdz", np.zeros(10), 0.0)
