"""완충재 동탄성계수 시험 성적서(PDF).

앱의 ReportPdfExporter 와 같은 양식이다 — A4 1장에 측정 정보 → 입력 조건 →
분석 결과 → 차트 → 각주. 같은 측정을 앱에서 뽑든 PC에서 뽑든 같은 서류가
나와야 한다.

판정값은 2시간 존치 측정값이 아니라 48시간 환산값이며, 그 차이가 성적서에서
가장 오해하기 쉬운 지점이라 환산식을 값 옆에 같이 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .analysis import AnalysisConfig, AnalysisResult
from .criteria import CORRECTION_48H, EXCELLENT_MAX_MN_M3, PASS_MAX_MN_M3, convert_48h, grade

PAGE_W, PAGE_H = A4
MARGIN = 40

INK = HexColor("#1A1A1A")
MUTED = HexColor("#6B6B6B")
RULE = HexColor("#BFBFBF")
PANEL = HexColor("#F2F4F6")
ACCENT = HexColor("#0B6E8A")
FAIL_RED = HexColor("#B3261E")

# 한글이 나오는 문서라 CJK 글꼴이 필요하다. 없으면 네모로 찍히므로, 흔한
# 경로를 훑어보고 실패하면 등록하지 않는다(그 경우 라틴 문자만 정상).
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/NanumGothic.ttf",
]

# 위 고정 경로에 없으면 흔한 글꼴 디렉터리를 훑는다. 배포판마다 경로가 달라
# 목록만으로는 자주 놓치고, 놓치면 한글이 조용히 네모로 찍힌다.
_FONT_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".fonts"),
    str(Path.home() / ".local/share/fonts"),
]
_FONT_NAME_HINTS = ("nanum", "notosanscjk", "notoserifcjk", "malgun", "applegothic", "spoqa")

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"


def _iter_font_candidates():
    for p in _FONT_CANDIDATES:
        yield Path(p)
    for d in _FONT_DIRS:
        root = Path(d)
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() in (".ttf", ".ttc", ".otf") and any(
                    h in p.name.lower().replace("-", "").replace("_", "")
                    for h in _FONT_NAME_HINTS
                ):
                    yield p
        except OSError:
            continue


def _register_font() -> None:
    global _FONT, _FONT_BOLD
    for path in _iter_font_candidates():
        if not path.exists():
            continue
        try:
            # .ttc 는 첫 서브폰트를 쓴다
            pdfmetrics.registerFont(TTFont("ReportKR", str(path), subfontIndex=0))
            _FONT = _FONT_BOLD = "ReportKR"   # 볼드 파일이 따로 없으면 같은 걸 쓴다
            return
        except Exception:
            continue


_register_font()


def korean_font_available() -> bool:
    """한글 글꼴을 찾았는지. UI가 사용자에게 경고할 수 있도록 노출한다."""
    return _FONT == "ReportKR"


@dataclass
class ReportMeta:
    site_name: str = ""
    unit_no: str = ""
    maker: str = ""
    product_name: str = ""
    lot_no: str = ""
    specimen_no: int = 1
    specimen_total: int = 3
    operator: str = ""


@dataclass
class ChartData:
    wave_t: np.ndarray = field(default_factory=lambda: np.array([]))
    wave_y: np.ndarray = field(default_factory=lambda: np.array([]))
    fft_x: np.ndarray = field(default_factory=lambda: np.array([]))
    fft_y: np.ndarray = field(default_factory=lambda: np.array([]))


def _dash(s: str) -> str:
    return s if s.strip() else "-"


def _sci(v: float) -> str:
    return f"{v:.1e}"


def build(
    path: str | Path,
    meta: ReportMeta,
    cfg: AnalysisConfig,
    result: AnalysisResult,
    thickness_m: float,
    axis: str,
    charts: ChartData | None = None,
    now: datetime | None = None,
) -> Path:
    """성적서를 쓴다. 값이 유효하지 않으면 만들지 않는다 — 숫자가 비거나 NaN인
    성적서가 나가는 것이 생성 실패보다 훨씬 나쁘다."""
    for name, v in (("f0", result.f0_hz), ("k'", result.k_prime_mn_m3), ("eta", result.eta)):
        if not np.isfinite(v):
            raise ValueError(f"{name} 값이 유효하지 않다: {v}")
    if result.f0_hz <= 0 or result.k_prime_mn_m3 <= 0:
        raise ValueError("f0/k' 가 0 이하다")

    path = Path(path)
    now = now or datetime.now()
    charts = charts or ChartData()

    s48 = convert_48h(result.k_prime_mn_m3)
    g = grade(s48)

    c = canvas.Canvas(str(path), pagesize=A4)
    y = PAGE_H - MARGIN

    # ── 제목 ────────────────────────────────────────────────────────────────
    c.setFillColor(INK)
    c.setFont(_FONT_BOLD, 17)
    c.drawString(MARGIN, y - 12, "완충재 동탄성계수 시험 성적서")
    c.setFont(_FONT, 9)
    c.setFillColor(MUTED)
    c.drawRightString(PAGE_W - MARGIN, y - 12, now.strftime("%Y-%m-%d %H:%M"))
    y -= 22
    c.setStrokeColor(INK)
    c.setLineWidth(1.2)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= 20

    y = _section(c, y, "측정 정보")
    y = _rows(
        c, y,
        [
            ("현장명", _dash(meta.site_name)),
            ("동·호수", _dash(meta.unit_no)),
            ("제조사", _dash(meta.maker)),
            ("품명", _dash(meta.product_name)),
            ("로트번호", _dash(meta.lot_no)),
            ("시편번호", f"{meta.specimen_no} / {meta.specimen_total}"),
            ("측정자", _dash(meta.operator)),
            ("측정일시", now.strftime("%Y-%m-%d %H:%M:%S")),
        ],
    )
    y -= 12

    y = _section(c, y, "입력 조건")
    y = _rows(
        c, y,
        [
            ("하중판 무게", f"{cfg.mass_kg:.2f} kg"),
            ("완충재 면적", f"{cfg.area_m2:.4f} m²"),
            ("완충재 두께", f"{thickness_m:.4f} m"),
            ("센서 축", axis),
            ("공진 탐색 범위", f"{cfg.search_low_hz:.0f} ~ {cfg.search_high_hz:.0f} Hz"),
            ("해석 방식", cfg.method),
        ],
    )
    y -= 12

    # ── 분석 결과 ───────────────────────────────────────────────────────────
    y = _section(c, y, "분석 결과")
    box_h = 62
    c.setFillColor(PANEL)
    c.rect(MARGIN, y - box_h, PAGE_W - 2 * MARGIN, box_h, stroke=0, fill=1)

    c.setFillColor(MUTED)
    c.setFont(_FONT, 9)
    c.drawString(MARGIN + 12, y - 18, "48시간 환산 동탄성계수 (판정값)")
    conv = f"= 측정값 {result.k_prime_mn_m3:.2f} × {CORRECTION_48H}"
    c.setFont(_FONT, 8.5)
    c.drawRightString(PAGE_W - MARGIN - 12, y - 18, conv)

    c.setFillColor(ACCENT)
    c.setFont(_FONT_BOLD, 22)
    c.drawString(MARGIN + 12, y - 44, f"{s48:.2f} MN/m³")

    c.setFillColor(FAIL_RED if g.name == "FAIL" else ACCENT)
    c.setFont(_FONT_BOLD, 20)
    c.drawRightString(PAGE_W - MARGIN - 12, y - 44, g.label)

    y -= box_h + 10
    y = _rows(
        c, y,
        [
            ("측정 동탄성계수 (2시간 존치)", f"{result.k_prime_mn_m3:.3f} MN/m³"),
            ("공진주파수 f", f"{result.f0_hz:.2f} Hz"),
            ("손실계수 (Loss Factor)", f"{result.eta:.3f}"),
        ],
        label_w=200,
    )
    y -= 14

    # ── 차트 ────────────────────────────────────────────────────────────────
    chart_w = (PAGE_W - 2 * MARGIN - 16) / 2
    chart_h = 132

    # 스펙트럼은 나이퀴스트까지 그리면 관심 대역이 몇 픽셀로 뭉개져 피크가
    # 보이지 않는다. 탐색 상한의 두 배까지만 자른다 — 범위 밖에 더 큰 피크가
    # 있는지 눈으로 확인할 여지는 남기면서 판정 대역은 읽히게.
    fx, fy = _crop_spectrum(charts.fft_x, charts.fft_y, cfg.search_high_hz * 2)

    _chart(c, MARGIN, y, chart_w, chart_h, "속도 파형 (분석 구간)",
           charts.wave_t, charts.wave_y, "time [s]", None)
    _chart(c, MARGIN + chart_w + 16, y, chart_w, chart_h, "주파수 스펙트럼",
           fx, fy, "frequency [Hz]", result.f0_hz)
    y -= chart_h + 34

    # ── 각주 ────────────────────────────────────────────────────────────────
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= 14
    c.setFillColor(MUTED)
    c.setFont(_FONT, 8)
    notes = [
        "동탄성계수 s′ = (2π f)² × m ÷ S  (m = 하중판 질량, S = 완충재 면적)",
        f"현장 측정은 하중판 2시간 존치 조건이며, 보정계수 {CORRECTION_48H}를 곱해 "
        "48시간 값으로 환산한다.",
        f"판정 기준 : {EXCELLENT_MAX_MN_M3:.0f} MN/m³ 이하 우수 / "
        f"{PASS_MAX_MN_M3:.0f} MN/m³ 이하 합격 / 초과 시 기준 초과",
        f"본 성적서는 시편 1개의 결과이며, 동일 로트 시편 {meta.specimen_total}개의 "
        "평균으로 최종 판정한다.",
    ]
    for note in notes:
        for line in simpleSplit(f"· {note}", _FONT, 8, PAGE_W - 2 * MARGIN):
            c.drawString(MARGIN, y, line)
            y -= 11

    c.showPage()
    c.save()
    return path


def _crop_spectrum(xs, ys, max_hz: float):
    """스펙트럼을 표시 상한까지 자른다. 자를 게 없으면 원본 그대로."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if xs.size == 0 or max_hz <= 0 or xs[-1] <= max_hz:
        return xs, ys
    n = int(np.searchsorted(xs, max_hz, side="right"))
    n = max(n, 2)
    return xs[:n], ys[:n]


def _section(c: canvas.Canvas, y: float, title: str) -> float:
    c.setFillColor(ACCENT)
    c.setFont(_FONT_BOLD, 11)
    c.drawString(MARGIN, y, title)
    y -= 6
    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    return y - 15


def _rows(c: canvas.Canvas, y: float, items, label_w: float = 78) -> float:
    """라벨-값을 2열로 흘려 그린다. label_w를 키우면 1열이 된다."""
    col_w = (PAGE_W - 2 * MARGIN) / 2
    two_col = label_w < 120
    i = 0
    while i < len(items):
        c.setFillColor(MUTED)
        c.setFont(_FONT, 9)
        c.drawString(MARGIN, y, items[i][0])
        c.setFillColor(INK)
        c.setFont(_FONT, 9.5)
        c.drawString(MARGIN + label_w, y, items[i][1])

        if two_col and i + 1 < len(items):
            c.setFillColor(MUTED)
            c.setFont(_FONT, 9)
            c.drawString(MARGIN + col_w, y, items[i + 1][0])
            c.setFillColor(INK)
            c.setFont(_FONT, 9.5)
            c.drawString(MARGIN + col_w + label_w, y, items[i + 1][1])
            i += 2
        else:
            i += 1
        y -= 15
    return y


def _chart(
    c: canvas.Canvas,
    x0: float,
    y_top: float,
    w: float,
    h: float,
    title: str,
    xs: np.ndarray,
    ys: np.ndarray,
    x_unit: str,
    marker_x: float | None,
) -> None:
    """단순 라인 차트. 축 눈금 대신 양 끝값만 적는다 — A4 한 장에 두 개를
    넣어야 해서 눈금 넣을 세로 여유가 없고, 정량 판단은 위 수치표가 한다."""
    c.setFillColor(INK)
    c.setFont(_FONT_BOLD, 9.5)
    c.drawString(x0, y_top, title)

    top = y_top - 8
    plot_h = h - 20
    bottom = top - plot_h

    c.setStrokeColor(RULE)
    c.setLineWidth(0.7)
    c.rect(x0, bottom, w, plot_h, stroke=1, fill=0)

    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if xs.size < 2 or ys.size != xs.size:
        c.setFillColor(MUTED)
        c.setFont(_FONT, 8)
        c.drawCentredString(x0 + w / 2, bottom + plot_h / 2, "no data")
        return

    y_min, y_max = float(ys.min()), float(ys.max())
    if y_min == y_max:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    x_min, x_max = float(xs[0]), float(xs[-1])
    span_x = x_max - x_min if x_max > x_min else 1.0

    def px(v: float) -> float:
        return x0 + (v - x_min) / span_x * w

    def py(v: float) -> float:
        return bottom + (v - y_min) / (y_max - y_min) * plot_h

    if y_min < 0 < y_max:
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(x0, py(0.0), x0 + w, py(0.0))

    # 가로 픽셀 수보다 점이 많으면 그릴 필요가 없다
    step = max(1, xs.size // max(1, int(w)))
    pts = [(px(xs[i]), py(ys[i])) for i in range(0, xs.size, step)]
    c.setStrokeColor(ACCENT)
    c.setLineWidth(0.9)
    p = c.beginPath()
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    c.drawPath(p)

    if marker_x is not None and x_min <= marker_x <= x_max:
        mx = px(marker_x)
        c.setStrokeColor(FAIL_RED)
        c.setLineWidth(0.8)
        c.line(mx, bottom, mx, top)
        c.setFillColor(FAIL_RED)
        c.setFont(_FONT, 7.5)
        # y축 최대값 라벨(top - 8)과 겹치지 않게 한 줄 내려 그린다. 마커가
        # 왼쪽 끝 근처면 두 라벨이 정확히 포개져 글자가 뒤엉킨다.
        c.drawString(min(mx + 3, x0 + w - 52), top - 20, f"f {marker_x:.1f} Hz")

    c.setFillColor(MUTED)
    c.setFont(_FONT, 7)
    c.drawString(x0 + 2, top - 8, _sci(y_max))
    c.drawString(x0 + 2, bottom + 2, _sci(y_min))
    base = bottom - 10
    c.drawString(x0, base, f"{x_min:.1f}")
    c.drawCentredString(x0 + w / 2, base, x_unit)
    c.drawRightString(x0 + w, base, f"{x_max:.1f}")
