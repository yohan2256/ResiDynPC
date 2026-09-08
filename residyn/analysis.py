"""동탄성계수 분석.

ResiDynMobile 의 DynAnalysis.kt 를 그대로 옮긴 것이다. 같은 측정에 대해 앱과
PC가 다른 값을 내면 어느 쪽도 신뢰할 수 없으므로, 단계 순서와 상수를 임의로
바꾸지 않는다.

절차: 가속도 전처리와 FFT로 초기 주파수 추정 → 창을 씌우지 않은 원시 자유감쇠
신호에 단일모드 지수감쇠 모델 적합. Hann 대역폭을 손실계수로 사용하지 않는다.
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
    correction_factor: float = 1.25
    excellent_max: float = 15.0
    pass_max: float = 20.0
    num_cycles: int = 10


@dataclass
class AnalysisResult:
    f0_hz: float
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

    if raw.ndim != 1 or raw.size > 96000 or not np.all(np.isfinite(raw)):
        raise ValueError("유효한 1차원 원시 데이터(최대 96000샘플)가 필요합니다")
    if not (100 <= fs <= 6400):
        raise ValueError("지원하지 않는 샘플레이트")
    if not (np.isfinite(cfg.mass_kg) and cfg.mass_kg > 0 and np.isfinite(cfg.area_m2) and cfg.area_m2 > 0):
        raise ValueError("질량과 면적은 양의 유한값이어야 합니다")
    if not (5 < cfg.search_low_hz < cfg.search_high_hz < fs / 2):
        raise ValueError("탐색 범위는 5 Hz 초과, Nyquist 미만의 오름차순이어야 합니다")
    if cfg.method.upper() not in ("FFT", "PEAK") or not (0 <= cfg.start_cycle <= 50 and 1 <= cfg.num_cycles <= 100):
        raise ValueError("분석 방식 또는 주기 설정이 올바르지 않습니다")
    if np.ptp(raw) < 8 or np.max(np.abs(raw)) >= 4090:
        raise ValueError("무신호/진폭 부족 또는 센서 포화: 다시 측정하세요")
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

    # Window bandwidth is NOT a damping estimator. Fit the unwindowed raw
    # free decay independently of the display/FFT cycle selection.
    f_d, alpha, fit_error = fit_decay(raw, fs, float(freq[idx_max]), cfg)
    f0 = float(np.hypot(f_d, alpha / (2 * np.pi)))
    eta = float(2 * alpha / (2 * np.pi * f0))  # equivalent viscous loss, 2*zeta

    omega = 2.0 * np.pi * f0
    k_prime = omega * omega * cfg.mass_kg / cfg.area_m2

    return AnalysisResult(
        f0_hz=f0,
        eta=float(eta),
        k_prime_mn_m3=float(k_prime / 1e6),
        freq=freq,
        mag=mag,
        vel_time=seg_v,
        time_axis=seg_t,
    )



def fit_decay(raw: np.ndarray, fs: float, seed: float, cfg: AnalysisConfig) -> tuple[float, float, float]:
    """Fit c + exp(-alpha*t)*(a*cos(2*pi*f*t)+b*sin(2*pi*f*t)).

    Equivalent viscous single-mode model, not a force-normalized FRF or an
    ISO compliance claim. Reject unresolved/multimode/non-decaying records.
    Same variable-projection coordinate search as DecayFit.kt.
    """
    # Seed from unwindowed raw acceleration, independent of display settings.
    center = float(np.median(raw))
    start = int(np.argmax(np.abs(raw - center)))
    pilot = raw[start:start + int(fs)] - center
    pad = _next_pow2(pilot.size * 4)
    spectrum = np.abs(np.fft.rfft(pilot, pad))
    lo = int(np.ceil(cfg.search_low_hz * pad/fs))
    hi = int(np.floor(cfg.search_high_hz * pad/fs))
    peak = lo + int(np.argmax(spectrum[lo:hi+1]))
    seed = peak * fs/pad
    period = max(1, int(fs / seed))
    levels = np.array([np.std(raw[i:i+period]) for i in range(0, raw.size-period+1, period)])
    strong = np.flatnonzero(levels > .2 * levels.max())
    if strong.size:
        first = int(strong[0])
        for i in range(first+3, levels.size):
            if levels[i] > .2*levels.max() and levels[i] > 1.5*levels[i-1]:
                raise ValueError("다중 충격 또는 비단조 감쇠: 한 번 타격하여 다시 측정하세요")
    count = min(raw.size - start, int(20 * fs / seed))
    if count < 3 * fs / seed:
        raise ValueError("자유감쇠 구간이 3주기보다 짧습니다")
    stride = max(1, int(fs / seed / 40))
    y = raw[start:start + count:stride]
    t = np.arange(y.size) * stride / fs
    energy = float(np.sum((y - y.mean()) ** 2))
    if energy < 1:
        raise ValueError("진동 에너지가 부족합니다")

    def score(f: float, alpha: float) -> float:
        if not (cfg.search_low_hz < f < cfg.search_high_hz) or not (0 <= alpha <= np.pi * seed):
            return float("inf")
        e = np.exp(-alpha * t)
        basis = np.column_stack((e * np.cos(2*np.pi*f*t), e * np.sin(2*np.pi*f*t), np.ones(y.size)))
        coef = np.linalg.lstsq(basis, y, rcond=None)[0]
        return float(np.sum((y - basis @ coef) ** 2) / energy)

    best = (float("inf"), seed, 0.0)
    for damping in (0.0, 0.02, 0.1, 0.3):
        f, alpha = seed, damping * np.pi * seed
        value = score(f, alpha)
        df, da = .04 * seed, .08 * np.pi * seed
        for _ in range(70):
            candidate = (value, f, alpha)
            for ff, aa in ((f-df,alpha),(f+df,alpha),(f,alpha-da),(f,alpha+da)):
                v = score(ff, aa)
                if v < candidate[0]: candidate = (v, ff, aa)
            if candidate[0] < value:
                value, f, alpha = candidate
            else:
                df *= .5
                da *= .5
            if df < seed * 1e-7 and da < seed * 1e-6: break
        if value < best[0]: best = (value, f, alpha)
    error, f, alpha = best
    if not np.isfinite(error) or error > .05:
        raise ValueError("단일 자유감쇠 모델 부적합: 다중 충격/모드/잡음 확인")
    # Require resolvable decay; a stationary tone must not certify damping.
    if alpha * (t[-1] - t[0]) < .1:
        raise ValueError("감쇠량이 부족하여 손실계수를 확정할 수 없습니다")
    if min(f-cfg.search_low_hz, cfg.search_high_hz-f) < max(.5, .01*f):
        raise ValueError("공진이 탐색 경계에 있습니다. 범위를 넓히세요")
    natural = np.hypot(f, alpha/(2*np.pi))
    if natural >= cfg.search_high_hz:
        raise ValueError("고유주파수가 탐색 범위를 벗어납니다")
    return float(f), float(alpha), float(error)
