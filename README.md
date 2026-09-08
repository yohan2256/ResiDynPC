> Measurement integrity update: see [MEASUREMENT_INTEGRITY.md](MEASUREMENT_INTEGRITY.md) for the new estimator, capture rejection and configurable judgment policy.

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

## 펌웨어 업데이트

**장치** 칸의 `펌웨어 업데이트` 버튼. 측정 중이든 아니든, 장치가 BOOTSEL
상태로 꽂혀 있어 포트가 아예 없든 상관없이 누를 수 있다.

패키지에 `fly_adxl345_firmware.uf2` 가 함께 들어 있어 기본값으로 그것을 쓰고,
`다른 파일 선택...` 으로 직접 빌드한 `.uf2` 를 지정할 수도 있다.

동작은 이렇다.

1. 부트로더 진입 매직 8바이트 전송 (이미 BOOTSEL 상태면 건너뜀)
2. `RPI-RP2` 볼륨이 뜰 때까지 대기
3. `.uf2` 복사
4. 측정용 CDC 포트가 돌아올 때까지 대기

PICOBOOT 프로토콜도, pyusb도, 윈도우에서 WinUSB 드라이버를 밀어 넣는 일도
필요 없다. BOOTSEL 상태의 RP2040은 그냥 USB 저장장치이고, 부트롬이 그 위에
쓰인 UF2를 받아 굽는다. (안드로이드 앱이 PICOBOOT를 직접 구현한 것은
안드로이드가 USB 대용량 저장장치를 마운트해 주지 않아서다.)

**벽돌이 되지 않는다.** 부트롬은 플래시가 아니라 마스크 ROM에 있어 BOOTSEL은
어떤 경우에도 살아 있다. 그래도 복사 전에 UF2를 검증하는데, 이유는 안전이
아니라 진단이다 — 잘못된 파일은 부트롬이 조용히 무시해서 "복사는 됐는데
아무 일도 없음"으로만 보인다.

볼륨이 안 뜨면 자동 마운트가 꺼져 있는 경우다. BOOTSEL을 누른 채 USB를 다시
꽂으면 된다.

## 왜 실측 ODR을 쓰는가

ADXL345 의 샘플레이트는 내부 RC 오실레이터에서 나와 개체별로 최대 ±5%
벗어난다. 동탄성계수는 `fs²` 로 스케일되므로 그 오차가 제곱으로 증폭된다.

펌웨어는 RP2040 크리스털(수십 ppm)로 실제 샘플 수를 세어 상태 프레임으로
약 1초마다 실측 ODR을 보낸다. 이 프로그램은 명목 3200 Hz 가 아니라 그
값을 쓴다.

**마지막 값 하나가 아니라 평균을 쓴다.** 창 하나에는 약 0.06%의 경계 오차가
있다 — 32샘플 버스트를 SPI로 읽는 동안 새 샘플이 FIFO에 들어오기 때문이다.
창마다 독립인 랜덤 오차라 N개를 평균하면 `1/√N`로 줄어들어, 30초 측정이면
0.01% 수준이 된다. 화면의 `실측 ODR` 표시가 값과 함께 평균에 들어간 창 수를
보여준다.

창 수가 늘지 않으면 펌웨어가 보고를 건너뛰고 있다는 뜻이다. FIFO 오버런이나
SPI 링크 이상이 있었던 창은 팝된 샘플만 세게 되어 ODR이 실제보다 낮게
나오므로, 펌웨어가 그런 창을 아예 보내지 않는다. 이때는 직전까지의 평균이
그대로 쓰이며, 데이터 프레임의 `OVERRUN` 플래그로 원인을 확인할 수 있다.

## 판정 기준과 환산

환산계수 및 우수/합격 상한을 입력 조건에서 설정합니다. 기본값은 기존값
(1.25, 15/20 MN/m³)을 유지합니다. 환산계수 1은 보정 없음입니다.
존치시간과 환산 근거는 프로그램이 자동 검증하지 않으며 적용 기준을 별도 확인해야 합니다.

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
| `firmware.py` | UF2 검증, RPI-RP2 볼륨 탐색, 펌웨어 쓰기 |
| `ui/` | 측정 창, 성적서 정보 입력, 펌웨어 업데이트 |

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

