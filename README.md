# ResiDyn PC

완충재 동탄성계수 측정기의 PC 버전. Fly-ADXL345-USB 장치에 USB로 붙어
가속도를 받고, 공진주파수·손실계수·동탄성계수를 계산해 시험 성적서(PDF)를
낸다. 안드로이드 앱 `ResiDynMobile` 과 같은 프로토콜·같은 분석·같은 성적서
양식을 쓴다 — 같은 측정을 어느 쪽에서 처리하든 같은 값과 같은 서류가 나와야
하기 때문이다.

## 설치

```bash
pip install -e ".[dev]"
```

Linux 에서 GUI를 띄우려면 Qt 런타임 라이브러리가 필요하다.

```bash
sudo apt install libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1
```

성적서에 한글이 나오므로 CJK 글꼴도 필요하다. 없으면 한글이 깨진 채로
출력되며, 앱이 저장 직후 경고를 띄운다.

```bash
sudo apt install fonts-nanum        # 또는 fonts-noto-cjk
```

## 실행

```bash
residyn          # 또는: python -m residyn
```

포트를 고르고 **측정 시작** → 충격 가진 → **정지** → **분석** 순으로 쓴다.
장치는 `2E8A:000A` 로 열거되며 포트 목록에서 맨 위에 올라온다.

Linux 에서 권한 오류가 나면 사용자를 `dialout` 그룹에 넣는다.

```bash
sudo usermod -aG dialout $USER      # 재로그인 필요
```

## 왜 실측 ODR을 쓰는가

ADXL345 의 샘플레이트는 내부 RC 오실레이터에서 나와 개체별로 최대 ±5%
벗어난다. 동탄성계수는 `fs²` 로 스케일되므로 그 오차가 제곱으로 증폭된다.

펌웨어는 RP2040 크리스털(수십 ppm)로 실제 샘플 수를 세어 상태 프레임으로
약 1초마다 실측 ODR을 보낸다. 이 프로그램은 명목 3200 Hz 가 아니라 그
값을 쓴다. 화면의 `실측 ODR` 표시로 실제 적용값을 확인할 수 있다.

## 판정은 48시간 환산값으로

현장 측정은 하중판을 2시간만 올려두지만 기준은 48시간 존치 값이다. 측정값에
`1.25` 를 곱한 뒤 판정한다.

| 48시간 환산값 | 판정 |
|---|---|
| ≤ 15 MN/m³ | 우수 |
| ≤ 20 MN/m³ | 합격 |
| > 20 MN/m³ | 기준 초과 |

이 구분을 놓치기 쉽다 — 측정값 13.27은 '우수' 구간이지만 환산값 16.59는
'합격' 구간이다. 성적서는 환산값을 판정값으로 크게 싣고 그 옆에 환산식을
같이 적는다.

## 원시 데이터 (.rdz)

내용은 **평범한 16-bit PCM mono WAV** 다. 확장자만 `.rdz` 로 두어 파일
관리자나 메신저가 오디오로 인식해 재생하려 들지 않게 한 것뿐이다. 앱이
저장한 파일과 같은 포맷이라 서로 열 수 있다.

```python
from scipy.io import wavfile
fs_header, x = wavfile.read("ResiDyn_Raw_20260817_231502.rdz")   # 확장자 무관
```

샘플은 스케일 없이 **ADXL345 원시 LSB** 다. `g = LSB × 0.0039`.

WAV 헤더의 샘플레이트는 정수만 담으므로, 정확한 실측 ODR은 `LIST/INFO` 주석
청크에 문자열로 들어 있다. **헤더값이 아니라 그 값을 써야 한다.**

```python
from residyn import rawio
rec = rawio.read("m.rdz")
rec.fs_hz            # 주석의 실측값 (없으면 헤더값)
rec.fs_from_comment  # 어느 쪽에서 왔는지
```

## 구조

| 모듈 | 하는 일 |
|---|---|
| `protocol.py` | 와이어 프레임 파서 (CRC16, 재동기화, 상태 프레임) |
| `serial_link.py` | USB CDC 링크, 리더 스레드, DTR |
| `analysis.py` | HPF → 적분 → FFT → f0 · 손실계수 · 동탄성계수 |
| `criteria.py` | 48시간 환산과 판정 |
| `rawio.py` | `.rdz` 읽기/쓰기 |
| `report.py` | 성적서 PDF |
| `ui/` | 측정 창, 성적서 정보 입력 |

## 테스트

```bash
QT_QPA_PLATFORM=offscreen pytest -q
```

프로토콜·분석·판정·파일입출력은 물론, 펌웨어 바이트에서 성적서 PDF까지
이어지는 end-to-end 경로와 GUI 스모크까지 포함한다.

## 알아둘 것

**DTR을 올려야 데이터가 온다.** 펌웨어의 `pico_stdio_usb` 는 쓰기 직전마다
`stdio_usb_connected()` 를 확인하고 그 판정이 DTR에서 나온다. DTR이 내려가
있으면 포트는 정상으로 열리고 리더 스레드도 도는데 수신 바이트만 0이 되는,
원인이 드러나지 않는 실패가 된다. `serial_link.py` 가 포트를 연 직후 올린다.

**부트로더 진입은 8바이트 매직이다.** `0xF0 0xB0 'R' 'E' 'B' 'O' 'O' 'T'`.
예전 2바이트(`0xF0 0xB0`)는 회선 잡음이 우연히 만들어낼 수 있어 장치가
예고 없이 PICOBOOT 모드로 넘어가는 사고가 가능했다.
