"""판정 기준.

성적서의 합불은 2시간 존치 측정값이 아니라 48시간 환산값으로 한다. 이 둘을
헷갈리면 성적서가 조용히 틀린 판정을 찍는다 — 예를 들어 측정값 13.27은
'우수' 구간이지만 환산값 16.59는 '합격' 구간이다.
"""

from __future__ import annotations

from enum import Enum

# 현장 측정은 하중판을 2시간만 올려두는데 기준은 48시간 존치 값이다.
CORRECTION_48H = 1.25

EXCELLENT_MAX_MN_M3 = 15.0   # 이하 '우수'
PASS_MAX_MN_M3 = 20.0        # 이하 '합격', 넘으면 '기준 초과'


class Grade(Enum):
    EXCELLENT = "우수"
    PASS = "합격"
    FAIL = "기준 초과"

    @property
    def label(self) -> str:
        return self.value


def convert_48h(measured_mn_m3: float) -> float:
    return measured_mn_m3 * CORRECTION_48H


def grade(mn_m3_for_48h: float) -> Grade:
    if mn_m3_for_48h <= EXCELLENT_MAX_MN_M3:
        return Grade.EXCELLENT
    if mn_m3_for_48h <= PASS_MAX_MN_M3:
        return Grade.PASS
    return Grade.FAIL
