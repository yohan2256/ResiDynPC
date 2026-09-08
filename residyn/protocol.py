"""Fly-ADXL345-USB 와이어 프로토콜 파서.

펌웨어가 CDC로 흘려보내는 바이트 스트림을 프레임으로 되돌린다. 명세는
fly_adxl345_firmware/PROTOCOL.md 이며, 이 모듈은 그 문서의 1~9절을 그대로
구현한다.

펌웨어는 호스트 연결 여부와 무관하게 계속 전송하므로, 포트를 여는 시점에
이미 진행 중이던 스트림 중간에 끼어들게 된다. 첫 바이트가 프레임 경계라는
보장이 없어 항상 0xFA로 재동기화한 뒤 파싱을 시작한다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SYNC1 = 0xFA
SYNC2_DATA = 0xCE
SYNC2_STATUS = 0xCD

DATA_HEADER_LEN = 8          # sync1 sync2 seq(2) count flags dropped(2)
STATUS_FRAME_LEN = 8         # sync1 sync2 odr_mhz(4) crc(2)
MAX_SAMPLES = 32
SAMPLE_LEN = 6

# 호스트 → 펌웨어
RESEND_SYNC = bytes((0xF0, 0x0D))
REBOOT_MAGIC = bytes((0xF0, 0xB0)) + b"REBOOT"

# flags 비트 (PROTOCOL.md 3절)
FLAG_OVERRUN = 0x01
FLAG_DROPPED = 0x02
FLAG_LINK_FAULT = 0x04

SENSITIVITY_G_PER_LSB = 0.0039   # ADXL345 FULL_RES

_MAX_BUFFER = 1 << 20            # 1 MiB 넘게 쌓이면 오래된 쪽을 버린다


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT (poly=0x1021, init=0xFFFF, 최종 XOR 없음)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_resend_request(seq: int) -> bytes:
    """재전송 요청 4바이트 (PROTOCOL.md 7절)."""
    return RESEND_SYNC + struct.pack("<H", seq & 0xFFFF)


@dataclass
class DataFrame:
    seq_start: int
    flags: int
    dropped: int
    samples: list[tuple[int, int, int]]   # (x, y, z) 원시 LSB

    discontinuity: bool = False

    @property
    def overrun(self) -> bool:
        return bool(self.flags & FLAG_OVERRUN)

    @property
    def link_fault(self) -> bool:
        return bool(self.flags & FLAG_LINK_FAULT)


@dataclass
class ParseStats:
    frames: int = 0
    status_frames: int = 0
    crc_errors: int = 0
    resync_bytes: int = 0     # 프레임 경계를 찾느라 버린 바이트
    gaps: int = 0             # seq 불연속 횟수
    dropped_samples: int = 0  # dropped 필드 누적


@dataclass
class FrameParser:
    """바이트를 넣으면 데이터 프레임을 돌려주는 스트림 파서.

    상태 프레임(0xFA 0xCD)은 실측 ODR로 흡수한다. 이 값은 헤더의 명목
    3200 Hz가 아니라 RP2040 크리스털로 잰 실제 샘플레이트이며, 물성치가
    fs²로 스케일되므로 분석에는 반드시 이 쪽을 써야 한다.

    받은 상태 프레임은 하나씩 쓰지 않고 평균 낸다. 창 하나에는 약 0.06%의
    경계 오차가 있는데(32샘플 버스트를 읽는 동안 새 샘플이 FIFO에 들어온다),
    창마다 독립인 랜덤 오차라 N개를 평균하면 1/√N로 줄어든다. 30초 측정이면
    0.01% 수준이다. 펌웨어가 창 길이를 1초 근방으로 고르게 맞춰 주므로 단순
    산술평균으로 충분하다.
    """

    stats: ParseStats = field(default_factory=ParseStats)

    quality_error: str | None = None

    _buf: bytearray = field(default_factory=bytearray, repr=False)
    _expected_seq: int | None = field(default=None, repr=False)
    _odr_sum_hz: float = field(default=0.0, repr=False)
    _odr_windows: int = field(default=0, repr=False)
    _odr_last_hz: float | None = field(default=None, repr=False)

    @property
    def measured_odr_hz(self) -> float | None:
        """분석에 쓸 실측 ODR — 지금까지 받은 창들의 평균. 없으면 None."""
        if self._odr_windows == 0:
            return None
        return self._odr_sum_hz / self._odr_windows

    @property
    def last_odr_hz(self) -> float | None:
        """가장 최근 창의 값. 표시·진단용이며 분석에는 쓰지 않는다."""
        return self._odr_last_hz

    @property
    def odr_windows(self) -> int:
        """평균에 들어간 창의 개수. 남은 오차를 가늠하는 데 쓴다."""
        return self._odr_windows

    def reset(self) -> None:
        self.quality_error = None
        self._buf.clear()
        self._expected_seq = None
        self._odr_sum_hz = 0.0
        self._odr_windows = 0
        self._odr_last_hz = None
        self.stats = ParseStats()

    def feed(self, chunk: bytes) -> list[DataFrame]:
        """수신 바이트를 넣고, 완성된 데이터 프레임들을 순서대로 돌려준다."""
        self._buf.extend(chunk)
        if len(self._buf) > _MAX_BUFFER:
            self.quality_error = "수신 버퍼 초과: 다시 측정하세요"
            del self._buf[: len(self._buf) - _MAX_BUFFER]

        out: list[DataFrame] = []
        while True:
            frame = self._try_one(out)
            if frame is None:
                break
        return out

    def _try_one(self, out: list[DataFrame]) -> bool | None:
        """프레임 하나를 소비하면 True, 더 필요한 바이트가 있으면 None."""
        buf = self._buf
        if len(buf) < 2:
            return None

        if buf[0] != SYNC1:
            # sync1까지 한 번에 건너뛴다 (한 바이트씩 지우면 O(n²))
            idx = buf.find(SYNC1, 1)
            if idx < 0:
                self.stats.resync_bytes += len(buf)
                buf.clear()
                return None
            self.stats.resync_bytes += idx
            del buf[:idx]
            return True

        kind = buf[1]
        if kind == SYNC2_DATA:
            return self._parse_data(out)
        if kind == SYNC2_STATUS:
            return self._parse_status()

        # 알 수 없는 sync2 — 1바이트만 버리고 재동기화
        self.stats.resync_bytes += 1
        del buf[:1]
        return True

    def _parse_data(self, out: list[DataFrame]) -> bool | None:
        buf = self._buf
        if len(buf) < DATA_HEADER_LEN:
            return None

        seq, count, flags, dropped = struct.unpack_from("<HBBH", buf, 2)
        if count == 0 or count > MAX_SAMPLES:
            # 헤더가 깨졌다. sync 2바이트만 버리고 다시 찾는다.
            self.quality_error = "손상된 프레임: 다시 측정하세요"
            self.stats.crc_errors += 1
            self.stats.resync_bytes += 2
            del buf[:2]
            return True

        total = DATA_HEADER_LEN + count * SAMPLE_LEN + 2
        if len(buf) < total:
            return None

        body = bytes(buf[: total - 2])
        got = struct.unpack_from("<H", buf, total - 2)[0]
        if crc16_ccitt(body) != got:
            self.quality_error = "손상된 프레임: 다시 측정하세요"
            self.stats.crc_errors += 1
            self.stats.resync_bytes += 2
            del buf[:2]
            return True

        samples = [
            struct.unpack_from("<hhh", buf, DATA_HEADER_LEN + i * SAMPLE_LEN)
            for i in range(count)
        ]

        gap = False
        if self._expected_seq is not None and seq != self._expected_seq:
            if ((seq - self._expected_seq) & 0xFFFF) >= 0x8000:
                # Duplicate/late retransmission must never append old samples.
                del buf[:total]
                return True
            gap = True
            self.stats.gaps += 1
            self.quality_error = "샘플 유실: 다시 측정하세요"
        self._expected_seq = (seq + count) & 0xFFFF
        if flags or dropped:
            self.quality_error = "센서/수집 오류: 다시 측정하세요"

        self.stats.frames += 1
        self.stats.dropped_samples += dropped
        out.append(DataFrame(seq, flags, dropped, samples, gap))
        del buf[:total]
        return True

    def _parse_status(self) -> bool | None:
        buf = self._buf
        if len(buf) < STATUS_FRAME_LEN:
            return None

        body = bytes(buf[: STATUS_FRAME_LEN - 2])
        got = struct.unpack_from("<H", buf, STATUS_FRAME_LEN - 2)[0]
        if crc16_ccitt(body) != got:
            # 상태 프레임은 재전송 대상이 아니다. 버리고 다음(최대 1초 뒤)을 기다린다.
            self.quality_error = "손상된 프레임: 다시 측정하세요"
            self.stats.crc_errors += 1
            self.stats.resync_bytes += 2
            del buf[:2]
            return True

        odr_hz = struct.unpack_from("<I", buf, 2)[0] / 1000.0
        if not 2500 <= odr_hz <= 4000:
            self.quality_error = "실측 ODR 범위 오류: 다시 측정하세요"
            del buf[:STATUS_FRAME_LEN]
            return True
        self._odr_last_hz = odr_hz
        self._odr_sum_hz += odr_hz
        self._odr_windows += 1
        self.stats.status_frames += 1
        del buf[:STATUS_FRAME_LEN]
        return True

