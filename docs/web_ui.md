# Native audio tuning WebUI

The optional WebUI runs inside LVA. It uses bundled HTML, CSS, and JavaScript without a frontend build or external assets.
It shares settings with Home Assistant and carries diagnostics through one authenticated WebSocket.
The peripheral API remains on its separate listener.

## Enable the listener

The WebUI is disabled by default.
Install the optional dependency for a source checkout:

```sh
.venv/bin/python -m pip install '.[webui]'
```

The Dockerfile installs this extra and includes the frontend assets.
Existing published images may lack this feature; use an image built from a commit containing the WebUI.

Create a password file with no group or other permissions.
Enter the password through an editor, rather than a command argument.
Make the file readable by the LVA process user.
The server accepts 1–1024 bytes and removes trailing CR/LF bytes.
It reads the file at startup; restart LVA after changing it.

**HTTP does not encrypt passwords or microphone audio.**
Use a localhost SSH tunnel for password login over an untrusted network.
Keep the listener on loopback unless direct LAN access is required.

Start LVA with these options alongside its normal audio configuration:

```sh
.venv/bin/python -m linux_voice_assistant \
  --web-ui-enabled \
  --web-ui-host 127.0.0.1 \
  --web-ui-port 6056 \
  --web-ui-password-file /path/to/private/webui-password
```

Open `http://127.0.0.1:6056/`.
For a remote host, forward its loopback listener:

```sh
ssh -N -L 16056:127.0.0.1:6056 user@voice-host
```

Open `http://127.0.0.1:16056/` while the tunnel is active.
The server accepts numeric bind addresses and loopback browser hosts with explicit ports.
Arbitrary hostnames are rejected.
HTTPS termination and reverse proxies require separate configuration; forwarded headers are not trusted.

### Docker environment

| Environment variable | CLI option | Default |
| --- | --- | --- |
| `WEB_UI_ENABLED` | `--web-ui-enabled` | `0` (disabled; set exactly `1` to enable) |
| `WEB_UI_HOST` | `--web-ui-host` | `127.0.0.1` |
| `WEB_UI_PORT` | `--web-ui-port` | `6056` |
| `WEB_UI_PASSWORD_FILE` | `--web-ui-password-file` | Unset |
| `WEB_UI_AUTH_BYPASS_CIDRS` | `--web-ui-auth-bypass-cidrs` | Empty |

Merge these entries into the existing Compose service's environment and volumes.
The repository uses list syntax for environment entries; convert that list to mapping syntax before merging this example.
Preserve every existing environment value and mount.
Do not replace the existing lists with this fragment.

```yaml
services:
  linux-voice-assistant:
    environment:
      WEB_UI_ENABLED: "1"
      WEB_UI_HOST: "127.0.0.1"
      WEB_UI_PORT: "6056"
      WEB_UI_PASSWORD_FILE: /run/secrets/lva-webui-password
    volumes:
      - ./webui-password:/run/secrets/lva-webui-password:ro
```

Preserve the existing audio mounts, preferences volume, image, and service settings when applying this fragment.
With host networking, no additional published port is needed.
A listener bound to container loopback requires an appropriate tunnel or host-network access.

## Authentication and settings

`WEB_UI_AUTH_BYPASS_CIDRS` accepts comma-separated IP addresses or strict CIDRs.
For example, `192.0.2.50/32,192.0.2.11/32` grants access to exactly those transport peers.
IPv6 addresses and CIDRs are supported.

**Every bypassed peer can change settings and receive microphone audio without a password.**
Use narrow entries for trusted clients.
A proxy entry grants bypass to requests arriving through that proxy.
LVA ignores `Forwarded` and `X-Forwarded-For`.
Sign out removes a password session; it does not revoke an IP bypass.

Startup requires a valid password file or at least one bypass entry.
Invalid configuration prevents WebUI startup and logs `WebUI disabled`; voice processing continues.
It does not fall back to anonymous access.
Sessions expire after one hour, reside only in memory, and disappear on restart.
Cookies use `HttpOnly` and `SameSite=Strict`; actual HTTPS requests also receive `Secure`.
Login, mutations, and WebSocket upgrades require an exact matching Origin.
The public page and scripts expose no settings or audio before authentication.

The controls update microphone volume (1–100), auto gain (0–31), noise suppression (0–4), primary model, primary threshold (0–1), and mute.
Slider values persist when committed.
The shared settings operation persists a candidate before publishing it to the detector and HA entities.
Failed writes show an error and preserve the previous values.
Existing CLI/environment startup overrides still apply.
Model selection uses available local models and preserves the secondary and stop configurations.
Selecting a model already used as the secondary is rejected.
Mute suppresses wake activation and voice input to HA; internal detector processing continues.

## Listen and inspect

1. Set Listening volume to zero before starting a silent check.
2. Click Start monitoring.
3. Select Input capture or Processed detector audio.
4. Inspect the levels, clipping count, threshold, and score markers.
5. Click Stop when finished.

Browser listening volume does not silence the LVA speaker.
For a fully silent check, mute or redirect the server's output sink separately.
Assistant mute stops diagnostics and cannot serve as an output-only mute.

Input capture contains channel 0 as float32 mono at 16 kHz, before LVA volume and WebRTC processing.
Microphone hardware and the operating system may already have processed it.
Processed audio contains the exact signed 16-bit PCM bytes supplied to wake-word feature extraction.
LVA uses its existing capture and inference; monitoring adds neither a second microphone nor a second detector.

Listening volume controls browser output only.
Browser playback can resample to the output device rate, which the page displays beside the source rate.
Playback starts after approximately 250 ms of buffered samples and schedules at most one second ahead.
Feed switching uses the current playback sample position.
The score cursor follows scheduled playback; it does not measure acoustic output latency.

Every primary openWakeWord score is exported, including misses.
microWakeWord exports available probabilities using the same strict `probability > threshold` comparison as detection.
A large point marks a threshold crossing; a vertical activation marker means LVA accepted a wake transition.
Refractory periods or an active assistant pipeline can suppress activation.
Without an HA connection, audio remains available while detector inference waits for HA.

Levels and clipping counts describe the latest received frame.
Raw samples with absolute value at least 1 and processed samples at either PCM rail count as clipped.
These counts do not identify distortion already introduced upstream.
The browser retains a rolling feed buffer and 30 seconds of scores in RAM.
LVA does not save recordings or diagnostic sessions.

Mute cancels scheduled playback and clears server queues and browser history.
Unmute, model changes, microphone volume changes, and WebRTC setting changes reset the diagnostic epoch.
Changing only the primary threshold updates the threshold and settings revision without resetting the audio epoch.
Gaps cancel playback and require buffering again.
Hidden pages, logout, and disconnect stop monitoring; reconnect requires another explicit Start action.

## Monitor protocol

The same authenticated `/api/ws` carries state snapshots, JSON controls, and binary audio.
Send exactly `{"command":"monitor_start"}` or `{"command":"monitor_stop"}`.
The start acknowledgement contains `active`, `stream_id`, `epoch`, `start_sample`, `sample_rate`, and both format names.
The stream ID identifies the server's monitor instance.
Epochs invalidate older diagnostic data within that instance.
Sample positions count captured channel-0 samples from the audio thread's startup.
Clients never receive samples before their acknowledged start position.

Each binary message begins with a 26-byte `!4sBBIQII` header.
Header integers use network byte order; audio payloads use little endian.

| Offset | Field | Meaning |
| --- | --- | --- |
| 0 | 4 bytes | ASCII `LVA1` |
| 4 | uint8 | Feed: 1 input float32, 2 processed s16 |
| 5 | uint8 | Bit 0: discontinuity |
| 6 | uint32 | Epoch |
| 10 | uint64 | First sample position |
| 18 | uint32 | Payload sample count |
| 22 | uint32 | Settings revision |
| 26 | Payload | `count × 4` or `count × 2` bytes |

A `scores` JSON message contains `epoch`, `revision`, `position_kind`, and `items`.
Each item contains `model`, `at_sample`, `probability`, `threshold`, `crossing`, and `accepted`.
`position_kind` is `available_after_processed_block`.
Scores share the consumed block boundary; this is not an exact feature-window timestamp.
WebRTC output is split into source spans to account for retained 10 ms frames.
After an observation loss, processed diagnostics can remain absent until a clean frame boundary.
This avoids assigning old buffered PCM to new samples.

`gap` messages report thread loss or a slow client; source intervals appear when known.
`reset` messages invalidate playback and history.
`detector` messages report whether an HA detector path is present.
Settings snapshots have no `type` field.

The thread handoff holds at most eight events and accepts at most 4096 captured samples per block.
Larger blocks continue through detection but cannot be monitored.
The audio thread uses nonblocking publication and schedules at most one pending drain.
Each WebSocket holds at most eight outgoing bundles.
Overflow flushes diagnostic backlog, reports a gap, and preserves required control messages.
Three overflows within ten seconds close the client with code 1013.
Socket sends time out after one second.
The server admits at most 16 sockets and 64 password sessions.
Login permits five attempts per peer per minute, with at most 1024 tracked peers.
HTTP JSON bodies are limited to 4096 bytes and five seconds; WebSocket messages are limited to 2048 bytes.
Session authorization is checked on commands and at most five seconds after an idle receive.

## Verification

Install development dependencies with `./script/setup --dev`.
This recreates `.venv` and includes the WebUI test dependency.
Run the required checks in order:

```sh
./script/lint_black --auto
./script/lint_isort --auto
./script/lint
./script/tests
node tests/browser/app.test.mjs
node tests/browser/monitor.test.mjs
```

The Node checks use native assertions and fake browser APIs; no frontend package install is required.
Python tests exercise the production audio loop with deterministic capture/model substitutes, real WebRTC framing, and authenticated HTTP/WebSockets.
They cover byte preservation, both detector families, backpressure, disconnect, session invalidation, mute, settings persistence, and sample gaps.
These checks do not establish microphone, speaker, model accuracy, or deployed performance.

Run the optional local benchmark from the repository root:

```sh
.venv/bin/python script/benchmark_monitor.py --mode disabled
.venv/bin/python script/benchmark_monitor.py --mode idle
.venv/bin/python script/benchmark_monitor.py --mode active
.venv/bin/python script/benchmark_monitor.py --mode slow
.venv/bin/python script/benchmark_monitor.py --mode disconnected
```

The benchmark calls the production audio loop using synthetic noise, real feature extractors, a selected local primary model, and Stop.
The default primary model is microWakeWord `okay_nabu`; `--model ok_nabu_v0.1` selects the bundled openWakeWord model.
`disabled` has no bus; `idle` has no subscriber; `active` drains each block; `slow` never drains; `disconnected` unsubscribes before capture.
It reports CPU/wall time, block timing percentiles, peak RSS, maximum queue size, activation counts, and a processed PCM digest.
Timing percentiles use the last 2000 completed blocks, keeping the measurement buffer bounded.
Block timings exclude synthetic recorder work and drain delivery; CPU/wall totals include them.
It omits real device I/O, network serialization, and browser work.
Use `--noise-suppression 1 --auto-gain 31` to include real WebRTC processing.
Use `--trace-memory` separately to inspect Python allocations; tracing affects timing and excludes native allocations.
Compare digests, activation counts, and pipeline state across modes before interpreting timing differences.

Before deployment acceptance, measure CPU, memory, and loop timing on the target with and without a subscriber.
Run 15 minutes of real monitoring and verify continued assistant operation.
Verify model selection, HA synchronization, gaps, disconnects, and mute on the target.
Keep listening volume zero during silent checks.
Coordinate audible playback and real wake attempts separately with the user.

### Local measurement record (2026-09-30)

The production Python code was Stage 2 commit `a0d3cbe6af01d2928c4ee6cad726c0bd1f664fd5`, exercised by the Stage 3 benchmark.
The host was macOS 27.0.1 ARM64 with Python 3.13.14.
Installed versions were NumPy 2.5.3, pymicro-wakeword 2.5.0, pyopen-wakeword 1.1.0, and webrtc-noise-gain 1.3.0.
Each mode ran three times with 2000 blocks, `--model ok_nabu_v0.1 --noise-suppression 1 --auto-gain 31`.
Each run processed 128 seconds of synthetic sample positions as quickly as possible.

| Mode | Median process CPU seconds | Median block p95, µs | Maximum queued events | Peak process RSS, MiB |
| --- | ---: | ---: | ---: | ---: |
| Disabled | 5.948 | 2088.1 | 0 | 120.8 |
| Idle | 5.658 | 1901.2 | 0 | 123.6 |
| Active | 5.774 | 1924.6 | 1 | 119.3 |
| Slow | 5.831 | 1982.1 | 8 | 116.4 |
| Disconnected | 5.687 | 1930.9 | 0 | 123.4 |

All 15 runs produced the same processed PCM digest, zero wake/stop calls, and an inactive final pipeline.
RSS includes imports and model initialization.
The modes ran separately; short-run timing differences do not establish a speedup or precise monitoring overhead.
Networking, microphone I/O, browser playback, and Radxa scheduling were absent.

Separate Python allocation traces used the default microWakeWord model without WebRTC.
Increasing from 2000 to 16000 blocks changed retained Python memory from 1,180,131 to 1,188,459 bytes in active mode.
Slow mode changed from 1,243,389 to 1,251,304 bytes; its queue stayed at eight events.
The harness retains only 2000 timings, so its history cannot grow with the workload.
The longer trace covered 1024 seconds of synthetic sample positions, rather than 15 minutes of real monitoring.
Native allocations and target runtime stability remain separate acceptance checks.
