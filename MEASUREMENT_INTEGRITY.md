# Measurement integrity update

## Changed behavior

- Analysis and raw export stop acquisition before taking a snapshot. A new capture or axis change invalidates the previous result. Axis changes clear single-axis data.
- CRC failures, sequence gaps, device flags, buffer overflow and invalid ODR latch a capture error. Restart measurement after fixing the cause. Late/duplicate frames are discarded; automatic retransmission is deliberately not used to certify an already incomplete capture.
- Live analysis requires measured ODR from the current connection. A device cannot inherit the previous device's rate. PC file imports use the file rate, close the serial connection and replace the capture.
- Zero/constant signals, saturation, non-finite input, invalid settings, insufficient decay, renewed excitation and poor single-mode fits cannot produce a passing report.
- Reports use a frozen analysis result, settings, axis and charts. The timestamp is the analysis timestamp; it is not reconstructed historical acquisition time for imported legacy files.

## Physical model and limits

The unwindowed raw acceleration is fitted to

`c + exp(-alpha*t) * (a*cos(2*pi*fd*t) + b*sin(2*pi*fd*t))`.

The fitted damped frequency is converted to `fn = hypot(fd, alpha/(2*pi))`;
`s' = (2*pi*fn)^2*m/S`; equivalent viscous loss is `eta = 2*alpha/(2*pi*fn)`.
Both legacy FFT and PEAK display modes use this same estimator. Display cycle
settings no longer define loss through Hann-window bandwidth. Removed result
fields `f1`/`f2` were window-dependent bandwidths, not measured material damping.

The fit uses up to 20 cycles from the strongest raw acceleration peak, a bounded
frequency/decay search and a normalized squared residual limit of 0.05. At least
3 cycles and alpha*T >= 0.1 are required. It assumes a dominant freely decaying
mode after excitation has ended. Force history, contact duration, mounting,
rocking modes and nonlinear materials still require physical validation. A
rejected measurement must not be interpreted as a low stiffness/pass result.
This is not an ISO certification or a force-normalized FRF measurement.

## Judgment settings

Correction factor and excellent/pass thresholds are editable and saved with the
analysis. Legacy defaults (1.25, 15 and 20 MN/m³) are retained for compatibility;
use factor 1 for no conversion. They are user criteria, not universal standards.
The app does not measure dwell time or validate a 2h-to-48h conversion. Confirm
applicable criteria and empirical conversion separately for each material.

## Verification

Regression tests include known decays, display-length independence, noise and
quantization, no-signal rejection, gaps/duplicates, state invalidation and invalid
UF2 rejection. Android core tests can run on the JVM; the CI workflow also builds
the Android app. USB hardware and field comparison against a reference instrument
remain required before deployment.

## Firmware

Both repositories bundle the same rebuilt RP2040 image. `firmware_manifest.json`
records image and source SHA-256, SDK, TinyUSB and compiler versions. The image was
cross-compiled but has not been flashed onto physical hardware in this review.
Only contiguous, complete, RP2040-family UF2 images within 2 MB flash are accepted.
Raw BIN and malformed/truncated/mixed-family images are rejected before erase.
FIFO overrun and SPI reconfiguration invalidate the ODR window until the next
clean window. Host-side capture error flags remain necessary even with this fix.
