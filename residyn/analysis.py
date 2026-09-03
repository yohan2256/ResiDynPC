"""동탄성계수 분석.

ResiDynMobile 의 DynAnalysis.kt 를 그대로 옮긴 것이다. 같은 측정에 대해 앱과
PC가 다른 값을 내면 어느 쪽도 신뢰할 수 없으므로, 단계 순서와 상수를 임의로
바꾸지 않는다.

절차: 가속도 환산 → HPF → 적분 → HPF → 충격점 탐지 → zero-crossing 구간 →
Hann + 10× zero-pad FFT → 탐색 범위 내 최대 스펙트럼 → 반전력 대역폭.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from .protocol import SENSITIVITY_G_PER_LSB

G = 9.80665

# DC·적분 drift 제거용 HPF 컷오프 [Hz].
#
# 공진 탐색 하한과는 별개다. 필터는 신호를 살려두는 쪽으로 낮게 두고, 어느
# 대역에서 f0를 찾을지는 탐색 범위로만 좁힌다 — 컷오프를 올려 탐색을 좁히면
# 통과대역 가장자리의 위상·진폭까지 함께 왜곡된다.
HPF_CUTOFF_HZ = 5.0

# 2차. 1차 RC는 감쇠가 6 dB/oct에 그쳐 적분이 증폭하는 저주파 drift를 충분히
# 누르지 못한다.
HPF_ORDER = 2


@dataclass(frozen=True)
class AnalysisConfig:
    area_m2: float = 0.04
    mass_kg: float = 8.0
    search_low_hz: float = 15.0
    search_high_hz: float = 150.0
    method: str = "FFT"          # "FFT" | "PEAK"
    start_cycle: int = 2
    num_cycles: int = 10


@dataclass
class AnalysisResult:
    f0_hz: float
    f1_hz: float
    f2_hz: float
    eta: float                   # 손실계수
    k_prime_mn_m3: float         # 동탄성계수 [MN/m³] (2시간 존치 측정값)
    freq: np.ndarray
    mag: np.ndarray
    vel_time: np.ndarray
    time_axis: np.ndarray


def highpass(x: np.ndarray, cutoff_hz: float, fs: float) -> np.ndarray:
    """2차 Butterworth 고역통과 (SOS).

    각 섹션의 상태를 첫 샘플의 정상상태로 초기화한다. 고역통과의 DC 이득은
    0이라 y[0]이 0이 되어, DC 오프셋이 큰 신호에서 생기는 시작 과도응답이
    사라진다. 이건 미관 문제가 아니다 — 뒤에서 충격 지점을 argmax(|acc|)로
    찾기 때문에, 과도응답이 충분히 크면 실제 충격 대신 그 지점이 잡힌다.
    """
    if x.size == 0:
        return x
    sos = butter(HPF_ORDER, cutoff_hz, btype="highpass", fs=fs, output="sos")
    zi = sosfilt_zi(sos) * x[0]
    y, _ = sosfilt(sos, x, zi=zi)
    return y


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def _find_peaks(x: np.ndarray, fs: float) -> list[int]:
    """PEAK 모드용 국소 최대점 (앱 findPeaks 와 동일 규칙)."""
    if x.size == 0:
        return []
    threshold = float(x.max()) * 0.1
    min_distance = int(fs / 200)
    peaks: list[int] = []
    last = -min_distance
    for i in range(1, x.size - 1):
        v = x[i]
        if v > x[i - 1] and v > x[i + 1] and v > threshold and (i - last) > min_distance:
            peaks.append(i)
            last = i
    return peaks


def _interp(freq: np.ndarray, mag: np.ndarray, i1: int, i2: int, target: float) -> float:
    y1, y2 = mag[i1], mag[i2]
    x1, x2 = freq[i1], freq[i2]
    if y2 == y1:
        return float((x1 + x2) / 2.0)
    return float(x1 + (target - y1) * (x2 - x1) / (y2 - y1))


def analyze(raw: np.ndarray, fs: float, cfg: AnalysisConfig) -> AnalysisResult:
    """원시 LSB 시계열 한 축을 받아 f0·손실계수·동탄성계수를 낸다.

    fs 는 상태 프레임의 실측 ODR을 쓴다. 명목 3200 Hz를 그대로 넣으면 결과가
    fs² 로 스케일되는 만큼 계통오차를 그대로 안는다.
    """
    raw = np.asarray(raw, dtype=float)
    if raw.size < 8:
        raise ValueError(f"샘플이 너무 적다: {raw.size}")
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError(f"샘플레이트가 올바르지 않다: {fs}")

    dt = 1.0 / fs

    # 1. 가속도 환산 → HPF
    acc = highpass(raw * SENSITIVITY_G_PER_LSB * G, HPF_CUTOFF_HZ, fs)

    # 2. 적분 → drift 제거 HPF
    vel = highpass(np.cumsum(acc) * dt, HPF_CUTOFF_HZ, fs)

    # 3. 충격 지점 = |acc| 최대
    idx_imp = int(np.argmax(np.abs(acc)))
    vel_tail = vel[idx_imp:]
    t_tail = np.arange(vel_tail.size) * dt

    # 4. zero-crossing 으로 분석 구간 잘라내기
    zc = np.nonzero(vel_tail[:-1] * vel_tail[1:] < 0)[0]
    seg_v, seg_t = vel_tail, t_tail
    if zc.size >= 5:
        i_s = int(zc[min(zc.size - 1, cfg.start_cycle * 2)])
        i_e = int(zc[min(zc.size - 1, (cfg.start_cycle + cfg.num_cycles) * 2)])
        if i_e > i_s:
            seg_v = vel_tail[i_s:i_e]
            seg_t = t_tail[i_s:i_e] - t_tail[i_s]

    # 5. Hann + 10× zero-padding FFT
    n = seg_v.size
    pad = _next_pow2(n * 10)
    windowed = seg_v * np.hanning(n) if n > 1 else seg_v
    spec = np.fft.rfft(windowed, pad)
    half = pad // 2
    mag = np.abs(spec)[:half]
    freq = np.arange(half) * fs / pad

    # 6. 탐색 범위 안에서 f0
    #
    # 범위는 필터 컷오프와 완전히 분리되어 있다. 범위가 뒤집히거나 스펙트럼
    # 밖으로 나가도 최소 1개 빈은 보도록 클램프한다.
    #
    # 하한은 올림, 상한은 버림이다. 둘 다 버리면 요청한 하한보다 아래 빈이
    # 탐색에 들어와, 범위를 15 Hz로 줘도 14 Hz대 피크가 f0로 뽑힌다. 빈 간격
    # (fs/pad)은 분석 구간이 짧을수록 커져서 그 어긋남이 1 Hz를 넘기도 한다.
    bins_per_hz = pad / fs
    i0 = int(np.clip(int(np.ceil(cfg.search_low_hz * bins_per_hz)), 1, half - 1))
    i1 = int(np.floor(cfg.search_high_hz * bins_per_hz)) if cfg.search_high_hz > 0 else half - 1
    i1 = int(np.clip(i1, i0, half - 1))

    idx_max = i0 + int(np.argmax(mag[i0 : i1 + 1]))
    max_v = float(mag[idx_max])
    f0 = float(freq[idx_max])

    # 7. PEAK 모드: 시계열 피크 간 평균 주기
    if cfg.method.upper() == "PEAK":
        peaks = _find_peaks(seg_v, fs)
        if len(peaks) >= 2:
            periods = np.diff(seg_t[peaks])
            avg = float(periods.mean())
            if avg > 0:
                f0 = 1.0 / avg

    # 8. 반전력 대역폭 → 손실계수
    hp = max_v / np.sqrt(2.0)
    il = idx_max
    while il > i0 and mag[il] > hp:
        il -= 1
    if il == idx_max:
        f1 = f0
    elif mag[il] > hp:
        # 하한까지 내려가도 반전력점을 못 찾음 — 외삽하면 엉뚱한 값이 되므로 클램프
        f1 = float(freq[il])
    else:
        f1 = _interp(freq, mag, il, il + 1, hp)

    ir = idx_max
    while ir < i1 and mag[ir] > hp:
        ir += 1
    if ir == idx_max:
        f2 = f0
    elif mag[ir] > hp:
        f2 = float(freq[ir])
    else:
        f2 = _interp(freq, mag, ir - 1, ir, hp)

    # 대역폭은 FFT 피크 기준으로 쟀으므로 eta도 FFT 피크로 정규화한다
    # (PEAK 모드에서 f0가 시계열 기반으로 바뀌어도 일관성 유지).
    f_fft = float(freq[idx_max])
    eta = (f2 - f1) / f_fft if f_fft > 0 else 0.0

    omega = 2.0 * np.pi * f0
    k_prime = omega * omega * cfg.mass_kg / cfg.area_m2

    return AnalysisResult(
        f0_hz=f0,
        f1_hz=f1,
        f2_hz=f2,
        eta=float(eta),
        k_prime_mn_m3=float(k_prime / 1e6),
        freq=freq,
        mag=mag,
        vel_time=seg_v,
        time_axis=seg_t,
    )
