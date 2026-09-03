"""원시 데이터 파일(.rdz) 입출력.

내용은 평범한 16-bit PCM mono WAV다. 확장자만 .rdz로 두어 파일 관리자나
메신저가 오디오로 인식해 재생하려 들지 않게 한 것뿐이고, scipy·soundfile·
MATLAB 모두 확장자와 무관하게 읽는다. 앱의 RawDataExporter 와 같은 포맷이라
안드로이드에서 뽑은 파일을 그대로 열 수 있다.

WAV 헤더의 샘플레이트는 정수만 담는다. 실측 ODR은 의도적으로 정확히 3200이
아니므로(상태 프레임의 존재 이유가 그 보정이다), 정확한 값은 LIST/INFO 주석
청크에 문자열로 남기고 읽을 때 그 쪽을 우선한다. 헤더만 믿으면 fs²로
스케일되는 물성치가 그 오차를 그대로 물려받는다.
"""

from __future__ import annotations

import re
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EXT = ".rdz"


@dataclass
class RawRecording:
    samples: np.ndarray        # 원시 LSB (int16 범위)
    fs_hz: float               # 실측 ODR (주석에 있으면 그 값, 없으면 헤더값)
    fs_from_comment: bool      # 정확한 값을 복원했는지
    axis: str | None
    comment: str


def _info_chunk(comment: str) -> bytes:
    body = comment.encode("ascii", errors="replace") + b"\0"
    pad = len(body) % 2
    payload = b"INFO" + b"ICMT" + struct.pack("<I", len(body)) + body + (b"\0" * pad)
    return b"LIST" + struct.pack("<I", len(payload)) + payload


def write(
    path: str | Path,
    samples: np.ndarray,
    fs_exact: float,
    axis: str = "AZ",
    extra: str = "",
) -> Path:
    """PCM16 mono WAV로 저장한다 (확장자는 호출자가 정한 그대로)."""
    path = Path(path)
    data = np.asarray(samples)
    if data.size == 0:
        raise ValueError("빈 데이터는 저장하지 않는다")
    if not np.isfinite(fs_exact) or fs_exact <= 0:
        raise ValueError(f"샘플레이트가 올바르지 않다: {fs_exact}")

    pcm = np.clip(np.rint(data), -32768, 32767).astype("<i2")

    comment = (
        "ResiDyn raw acceleration. Container is a plain 16-bit PCM mono WAV; "
        f"the {EXT} extension only stops players from picking it up. "
        f"fs_exact={fs_exact:.4f} Hz (header rate is rounded - use this value). "
        f"axis={axis} unit=raw_LSB g=LSB*0.0039 (ADXL345 FULL_RES +/-16g) "
        f"samples={pcm.size}"
    )
    if extra:
        comment += " " + extra

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(max(1, int(round(fs_exact))))
        w.writeframes(pcm.tobytes())

    # wave 모듈은 INFO 청크를 못 쓰므로 data 청크 뒤에 직접 덧붙이고
    # RIFF 크기를 갱신한다. 청크를 스캔하는 리더는 그대로 읽는다.
    blob = bytearray(path.read_bytes())
    blob.extend(_info_chunk(comment))
    struct.pack_into("<I", blob, 4, len(blob) - 8)
    path.write_bytes(bytes(blob))
    return path


def _find_comment(blob: bytes) -> str:
    """RIFF 청크 체인을 훑어 LIST/INFO 의 ICMT 본문을 찾는다."""
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return ""
    p = 12
    while p + 8 <= len(blob):
        cid = blob[p : p + 4]
        size = struct.unpack_from("<I", blob, p + 4)[0]
        body = blob[p + 8 : p + 8 + size]
        if cid == b"LIST" and body[:4] == b"INFO":
            q = 4
            while q + 8 <= len(body):
                sid = body[q : q + 4]
                ssize = struct.unpack_from("<I", body, q + 4)[0]
                if sid == b"ICMT":
                    return body[q + 8 : q + 8 + ssize].split(b"\0")[0].decode(
                        "ascii", errors="replace"
                    )
                q += 8 + ssize + (ssize % 2)
        p += 8 + size + (size % 2)
    return ""


def read(path: str | Path) -> RawRecording:
    """.rdz(=WAV)를 읽는다. 실측 ODR은 주석에 있으면 그 값을 쓴다."""
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"16-bit PCM이 아니다: {w.getsampwidth() * 8}-bit")
        channels = w.getnchannels()
        header_fs = float(w.getframerate())
        frames = w.readframes(w.getnframes())

    samples = np.frombuffer(frames, dtype="<i2").astype(float)
    if channels > 1:
        # 다채널이면 첫 채널만 쓴다 (앱은 mono로 쓰지만 방어적으로)
        samples = samples.reshape(-1, channels)[:, 0]

    comment = _find_comment(path.read_bytes())
    fs, from_comment = header_fs, False
    m = re.search(r"fs_exact=([0-9]+(?:\.[0-9]+)?)", comment)
    if m:
        fs, from_comment = float(m.group(1)), True

    m = re.search(r"axis=(\w+)", comment)
    axis = m.group(1) if m else None

    return RawRecording(samples, fs, from_comment, axis, comment)
