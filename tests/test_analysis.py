"""분석 검증.

앱(DynAnalysis.kt)과 같은 값을 내야 하므로, 알려진 입력에 대한 결과를 못박아
둔다. 특히 탐색 범위가 실제로 탐색을 가르는지, HPF가 시작 과도응답을 만들지
않는지를 확인한다 — 후자는 충격 지점 탐지가 argmax(|acc|)라 무해하지 않다.
"""

import numpy as np
import pytest

from residyn.analysis import (
    HPF_CUTOFF_HZ,
    AnalysisConfig,
    analyze,
    highpass,
)

FS = 3200.0


def damped_sine(f: float, n: int, amp: float = 1000.0, damping: float = 5.0) -> np.ndarray:
    t = np.arange(n) / FS
    return amp * np.exp(-damping * t) * np.sin(2 * np.pi * f * t)


def test_fft_finds_resonance():
    r = analyze(damped_sine(100.0, 6400), FS, AnalysisConfig())
    assert r.f0_hz == pytest.approx(100.0, abs=5.0)
    assert r.eta > 0.0


def test_peak_method_finds_resonance():
    cfg = AnalysisConfig(method="PEAK", num_cycles=4)
    r = analyze(damped_sine(80.0, 6400), FS, cfg)
    assert r.f0_hz == pytest.approx(80.0, abs=5.0)


def test_k_prime_formula_and_units():
    """k' = (2πf0)²·m/S, 결과는 MN/m³."""
    r = analyze(damped_sine(100.0, 6400), FS, AnalysisConfig(mass_kg=8.0, area_m2=0.04))
    expected = (2 * np.pi * r.f0_hz) ** 2 * 8.0 / 0.04 / 1e6
    assert r.k_prime_mn_m3 == pytest.approx(expected, rel=1e-9)
    # 100 Hz 부근이면 79 MN/m³ 언저리 — 단위를 1e6 잘못 두면 여기서 걸린다
    assert 60.0 < r.k_prime_mn_m3 < 100.0


def test_search_range_confines_f0():
    """상한 밖 공진은 f0로 뽑히면 안 된다."""
    with pytest.raises(ValueError):
        analyze(damped_sine(200.0, 6400), FS, AnalysisConfig(search_high_hz=150.0))


def test_widening_range_reaches_higher_resonance():
    """앞 테스트와 짝 — 범위가 결과를 클램프하는 게 아니라 탐색을 가른다."""
    r = analyze(damped_sine(200.0, 6400), FS, AnalysisConfig(search_high_hz=500.0))
    assert r.f0_hz == pytest.approx(200.0, abs=5.0)


def test_inverted_range_is_rejected():
    cfg = AnalysisConfig(search_low_hz=200.0, search_high_hz=50.0)
    with pytest.raises(ValueError):
        analyze(damped_sine(100.0, 6400), FS, cfg)


@pytest.mark.parametrize("n", [300, 400, 480, 500, 600, 800])
@pytest.mark.parametrize("f", [2.0, 10.0, 20.0, 50.0])
def test_short_or_sparse_signals_require_resolvable_decay(n, f):
    if f < 15 or n / FS < 3 / f + .03:
        with pytest.raises(ValueError):
            analyze(damped_sine(f, n), FS, AnalysisConfig())
        return
    r = analyze(damped_sine(f, n), FS, AnalysisConfig())
    assert np.isfinite(r.f0_hz) and np.isfinite(r.eta)


def test_impulse_only_signal():
    x = np.zeros(600)
    x[10] = 1000.0
    with pytest.raises(ValueError):
        analyze(x, FS, AnalysisConfig())


def test_too_few_samples_rejected():
    with pytest.raises(ValueError):
        analyze(np.zeros(4), FS, AnalysisConfig())


def test_bad_sample_rate_rejected():
    with pytest.raises(ValueError):
        analyze(damped_sine(100.0, 1000), 0.0, AnalysisConfig())


# ---- 고역통과 필터 ----------------------------------------------------------

def test_highpass_removes_dc_without_startup_transient():
    """DC 상수 입력의 출력은 전 구간 0이어야 한다.

    상태를 0으로 두면 시작에서 큰 과도응답이 생기고, 충격 지점을
    argmax(|acc|)로 찾기 때문에 실제 충격 대신 그 지점이 잡힌다.
    """
    y = highpass(np.full(2000, 7.5), HPF_CUTOFF_HZ, FS)
    assert np.max(np.abs(y)) < 1e-9


def test_highpass_is_second_order():
    """컷오프 아래에서 옥타브당 약 12 dB로 떨어져야 한다 (1차면 6 dB)."""
    def amp_at(f):
        n = 1 << 16
        t = np.arange(n) / FS
        y = highpass(np.sin(2 * np.pi * f * t), HPF_CUTOFF_HZ, FS)
        return np.abs(y[n // 2 :]).max()   # 과도구간 제외

    slope_db = 20 * np.log10(amp_at(2.5) / amp_at(1.25))
    assert 10.5 < slope_db < 13.5, f"{slope_db:.2f} dB/oct"


def test_highpass_passes_band_of_interest():
    """탐색 대역(15~150 Hz)은 거의 손대지 않아야 한다."""
    n = 1 << 16
    t = np.arange(n) / FS
    for f in (15.0, 50.0, 150.0):
        y = highpass(np.sin(2 * np.pi * f * t), HPF_CUTOFF_HZ, FS)
        gain = np.abs(y[n // 2 :]).max()
        assert gain > 0.95, f"{f} Hz 에서 이득 {gain:.3f}"


def test_measured_odr_changes_result():
    """fs를 명목값으로 두면 결과가 fs²만큼 어긋난다 — 실측값을 써야 하는 이유."""
    x = damped_sine(100.0, 6400)
    nominal = analyze(x, 3200.0, AnalysisConfig())
    measured = analyze(x, 3168.0, AnalysisConfig())      # -1%
    ratio = measured.k_prime_mn_m3 / nominal.k_prime_mn_m3
    assert ratio == pytest.approx((3168.0 / 3200.0) ** 2, rel=0.02)



@pytest.mark.parametrize("eta", [.01, .05, .1, .2, .4])
@pytest.mark.parametrize("cycles", [5, 10, 20])
def test_known_decay_accuracy_independent_of_window(eta, cycles):
    t = np.arange(9600) / FS
    age = np.maximum(t-.2, 0)
    x = np.where(t >= .2, 1000*np.exp(-np.pi*50*eta*age)*np.sin(2*np.pi*50*age), 0)
    r = analyze(x, FS, AnalysisConfig(num_cycles=cycles))
    assert r.f0_hz == pytest.approx(50*np.sqrt(1+(eta/2)**2), abs=.002)
    assert r.eta == pytest.approx(eta/np.sqrt(1+(eta/2)**2), abs=.0001)

@pytest.mark.parametrize("kind", ["zero", "constant", "tone", "nan", "clip", "double"])
def test_invalid_measurement_cannot_produce_a_grade(kind):
    t = np.arange(9600)/FS
    x = 1000*np.exp(-5*t)*np.sin(2*np.pi*50*t)
    if kind == "zero": x[:] = 0
    if kind == "constant": x[:] = 255
    if kind == "tone": x = 1000*np.sin(2*np.pi*50*t)
    if kind == "nan": x[200] = np.nan
    if kind == "clip": x[200] = 4095
    if kind == "double": x[640:] += 800*np.exp(-5*t[:-640])*np.sin(2*np.pi*50*t[:-640])
    with pytest.raises(ValueError): analyze(x, FS, AnalysisConfig())

def test_decay_fit_tolerates_sensor_quantization_and_small_noise():
    t=np.arange(9600)/FS
    rng=np.random.default_rng(42)
    x=np.rint(255+1000*np.exp(-np.pi*50*.1*t)*np.sin(2*np.pi*50*t)+rng.normal(0,1,t.size))
    r=analyze(x,FS,AnalysisConfig())
    assert r.eta == pytest.approx(.1/np.sqrt(1+.05**2), abs=.002)
