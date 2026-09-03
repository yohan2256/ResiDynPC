"""펌웨어 바이트 → 파싱 → 분석 → 성적서까지 한 번에 흘려보는 검증.

각 모듈이 따로 맞아도 이어붙이면 어긋나는 곳이 있다 — 실측 ODR이 분석까지
전달되는지, 성적서 판정이 환산값에서 나오는지 같은 것들이다.
"""

import struct
import wave

import numpy as np
import pytest

from residyn import rawio, report
from residyn.analysis import AnalysisConfig, analyze
from residyn.criteria import Grade, convert_48h, grade
from residyn.protocol import FrameParser, crc16_ccitt

FS_TRUE = 3187.421          # 명목 3200이 아닌 실측 ODR


def _data_frame(seq: int, samples) -> bytes:
    body = struct.pack("<BBHBBH", 0xFA, 0xCE, seq, len(samples), 0, 0)
    for x, y, z in samples:
        body += struct.pack("<hhh", x, y, z)
    return body + struct.pack("<H", crc16_ccitt(body))


def _status_frame(odr_hz: float) -> bytes:
    body = struct.pack("<BBI", 0xFA, 0xCD, int(round(odr_hz * 1000)))
    return body + struct.pack("<H", crc16_ccitt(body))


def _firmware_stream(signal: np.ndarray, chunk: int = 32) -> bytes:
    """펌웨어가 보낼 법한 스트림: 상태 프레임이 데이터 사이에 섞여 온다."""
    out = bytearray(_status_frame(FS_TRUE))
    seq = 0
    for i in range(0, signal.size, chunk):
        block = signal[i : i + chunk]
        out += _data_frame(seq, [(0, 0, int(v)) for v in block])
        seq = (seq + block.size) & 0xFFFF
        if i % (chunk * 100) == 0 and i:
            out += _status_frame(FS_TRUE)
    return bytes(out)


@pytest.fixture
def captured():
    """감쇠 진동을 펌웨어 프레임으로 감싸 파싱까지 마친 상태."""
    t = np.arange(6400) / FS_TRUE
    # 펌웨어가 실제로 보내는 것은 int16이므로, 정수화를 먼저 하고 그 값을
    # 프레임 구성과 기대값 양쪽에 함께 쓴다.
    signal = np.rint(1200.0 * np.exp(-5.0 * t) * np.sin(2 * np.pi * 42.0 * t))

    parser = FrameParser()
    blob = _firmware_stream(signal)

    z: list[int] = []
    # USB 읽기처럼 임의 크기로 잘라 넣는다
    for i in range(0, len(blob), 777):
        for frame in parser.feed(blob[i : i + 777]):
            z.extend(s[2] for s in frame.samples)

    return parser, np.array(z, dtype=float), signal


def test_stream_roundtrip_is_lossless(captured):
    parser, z, signal = captured
    assert parser.stats.crc_errors == 0
    assert parser.stats.gaps == 0
    assert np.array_equal(z, signal)


def test_measured_odr_reaches_analysis(captured):
    parser, z, _ = captured
    assert parser.measured_odr_hz == pytest.approx(FS_TRUE, abs=1e-3)

    r = analyze(z, parser.measured_odr_hz, AnalysisConfig())
    assert r.f0_hz == pytest.approx(42.0, abs=2.0)


def test_nominal_rate_biases_the_result(captured):
    """명목 3200을 쓰면 결과가 (실측/명목)² 만큼 어긋난다."""
    parser, z, _ = captured
    good = analyze(z, parser.measured_odr_hz, AnalysisConfig())
    bad = analyze(z, 3200.0, AnalysisConfig())
    assert bad.k_prime_mn_m3 != pytest.approx(good.k_prime_mn_m3, rel=1e-6)
    assert bad.k_prime_mn_m3 / good.k_prime_mn_m3 == pytest.approx(
        (3200.0 / FS_TRUE) ** 2, rel=0.05
    )


def test_raw_file_roundtrip_reproduces_analysis(tmp_path, captured):
    """앱이 뽑은 .rdz를 PC에서 열어도 같은 결과가 나와야 한다."""
    parser, z, _ = captured
    fs = parser.measured_odr_hz

    p = rawio.write(tmp_path / "m.rdz", z, fs, axis="AZ")
    rec = rawio.read(p)

    assert rec.fs_from_comment is True
    assert rec.fs_hz == pytest.approx(fs, abs=1e-3)

    a = analyze(z, fs, AnalysisConfig())
    b = analyze(rec.samples, rec.fs_hz, AnalysisConfig())
    assert b.f0_hz == pytest.approx(a.f0_hz, rel=1e-9)
    assert b.k_prime_mn_m3 == pytest.approx(a.k_prime_mn_m3, rel=1e-9)


def test_report_is_written_and_is_a_pdf(tmp_path, captured):
    parser, z, _ = captured
    cfg = AnalysisConfig()
    r = analyze(z, parser.measured_odr_hz, cfg)

    out = report.build(
        tmp_path / "cert.pdf",
        report.ReportMeta(
            site_name="동탄 ○○지구 A블록", unit_no="103동 1204호",
            maker="○○산업", product_name="EPS 완충재 30T",
            lot_no="L-260814-03", specimen_no=2, specimen_total=3,
            operator="성요한",
        ),
        cfg, r, thickness_m=0.03, axis="AZ",
        charts=report.ChartData(r.time_axis, r.vel_time, r.freq, r.mag),
    )

    blob = out.read_bytes()
    assert blob.startswith(b"%PDF-")
    assert blob.rstrip().endswith(b"%%EOF")
    assert len(blob) > 2000


def test_report_refuses_invalid_result(tmp_path, captured):
    """숫자가 비거나 NaN인 성적서가 나가는 것이 생성 실패보다 나쁘다."""
    parser, z, _ = captured
    cfg = AnalysisConfig()
    r = analyze(z, parser.measured_odr_hz, cfg)

    r.f0_hz = float("nan")
    with pytest.raises(ValueError):
        report.build(tmp_path / "bad.pdf", report.ReportMeta(), cfg, r, 0.03, "AZ")


def test_verdict_comes_from_converted_value(captured):
    """성적서 판정은 2시간 측정값이 아니라 48시간 환산값 기준이다."""
    measured = 13.273
    assert grade(measured) is Grade.EXCELLENT
    assert grade(convert_48h(measured)) is Grade.PASS


def test_written_raw_file_opens_in_plain_wave_module(tmp_path, captured):
    """확장자만 바꾼 것이므로 표준 도구가 그대로 읽어야 한다."""
    _, z, _ = captured
    p = rawio.write(tmp_path / "n.rdz", z, FS_TRUE)
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == z.size
