---
title: '事件录像中的识别目标框'
type: 'feature'
created: '2026-10-19'
status: 'done'
baseline_commit: '19a07f595eebdef735c37f7c415816c68da71e08'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Event videos lack target boxes; preview overlays do not survive WeChat playback.

**Approach:** Burn boxes into new event MP4s using the existing detector. Add “事件录像显示目标框” and color selection. Default on, including configurations missing the new field; changes apply to the next event. Playback, downloads and WeChat share the completed video.

## Boundaries & Constraints

**Always:** Follow [FigJam](https://www.figma.com/board/ArpiLblMIaOACjCxYDzZYu/?node-id=0-1), “单模型 / 单标签 / 生物”: default red `#F51616`, manual alternatives orange `#E86D08` / dark blue `#0931C0` for background contrast. Draw outlines, no fill; one selected color, no invented severity/confidence meanings.

Preserve event booleans, thresholds, cooldown and leave timing. Do not mutate Hub detector state, preview or timelapse pixels. Local cameras remain single-open. Enabled annotation re-encodes shared preview-resolution JPEGs to H.264 MP4 for both sources: RTSP resolution may drop (normally 480 pixels wide), CPU increases. Show this beside the switch. Disabled preserves previous recording paths, including RTSP high-resolution recording.

**Ask First:** New recognition model, continuous tracking, changed event rules or different mandatory quality tradeoff.

**Never:** Claim vehicle/animal/identity recognition; call all contours confirmed people; reuse coordinates on another frame; retrofit old videos; publish to GitHub or start remote exposure.

## I/O & Edge-Case Matrix

| State | Expected behavior | Failure handling |
|---|---|---|
| New event, switch on | Freeze options; burn frame-matched confirmed boxes | Reject invalid settings before any write |
| Haar / auto / motion | Actual Haar ROI offsets / matching heuristic candidate / motion regions respectively | Motion regions must not be described as verified people |
| Warmup, negative, stale, offline or changed source/size | No inherited boxes; reset independent annotation state | Never carry another source's coordinates |
| Mid-event settings change | Applies next event | No mixed modes in one file |
| Annotation fails | Preserve raw video where possible; visible warning | Do not claim annotation succeeded |
| Encoder/validation fails | Hide unfinished/failed output from playback/WeChat | Log error, no corrupt completed MP4 |

</frozen-after-approval>

## Code Map

- `nvr/detect.py`: boolean detector, foreground candidates and shared Haar singleton.
- `nvr/hub.py`, `nvr/camera.py`: sampled detection, capture cache and event lifecycle.
- `nvr/local_camera.py`, `nvr/ffmpeg.py`: bounded JPEG encoder and timestamp handling.
- `nvr/config.py`, `nvr/server.py`, `web/index.html`, `web/app.js`: settings and validation.
- `nvr/recordings.py`, `nvr/clawbot.py`: existing completed-video query/send flow.

## Tasks & Acceptance

**Execution:**
- [x] `nvr/detect.py`: expose confirmed boxes/provenance without changing booleans; annotation owns independent MOG2/Haar state using the same XML.
- [x] `nvr/camera.py`, `nvr/hub.py`: atomically pair frame/sequence/metadata; use the paired trigger frame at event start where available. Never share the live Hub detector with encoding threads.
- [x] `nvr/event_overlay.py` (new): process each new JPEG independently, clip boxes, cache at most one result. Duplicate sequence reuses it; negative clears boxes; source pixels remain untouched.
- [x] `nvr/local_camera.py`, `nvr/camera.py`, `nvr/ffmpeg.py`: shared-frame encoding for annotated events; bounded writes, wall-clock timestamps, no backlog. Require `.part.mp4`, successful decode validation and atomic publication; preserve disabled paths.
- [x] `nvr/config.py`, `nvr/server.py`, `web/index.html`, `web/app.js`: strict boolean `record.target_boxes` (true, including missing-field migration) and enum `record.target_box_color` (three exact colors, default red), validated before mutation. Chinese controls, quality/CPU notice and visible annotation warning.
- [x] `tests/test_event_overlay.py` (new), `tests/run.py`: matrix, ROI offsets/clipping, independent state, frame pairing, all colors. Compare baseline event decisions across existing modes.
- [x] `tests/test_local_camera.py`, `tests/test_media.py`, `tests/test_clawbot_events.py`: both sources/switches, single-open, blocked/failed encoding, decoded MP4 colored pixels and fake-CDN annotated-video round trip.
- [x] `docs/WECHAT_QUICK_START.txt`, `docs/VALIDATION.md`, `README.md`: operation, limits and evidence. Build a staged local Windows candidate; preserve formal installation.

**Acceptance Criteria:**
- Given either source with annotation on, when an event finishes, then decoded MP4 contains selected-color outlines at confirmed locations and plays with wall-clock-consistent duration.
- Given baseline inputs/settings, when annotation runs, then event decisions remain equivalent and capture stays responsive despite slow encoding.
- Given a completed annotated event, when sending “今天事件” then “播放第一条”, then WeChat sends that video with boxes retained.
- Given the rebuilt EXE, when the user checks real recording/local/WeChat playback, then record observations separately from synthetic checks; unconfirmed behavior remains unverified.

## Spec Change Log

- 2026-10-19: User approved development with the explicit change “新增画框开关，默认打开”. Updated the default and missing-field migration to enabled before freezing intent; other boundaries preserved.

## Design Notes

`Detector.boxes` includes unconfirmed contours: do not use it directly. Independent annotation preserves Hub background-learning cadence. Warmup/negative frames can lack boxes; stationary people are not continuously tracked. Configured FPS is best effort.

## Verification

- Main review patch execution, 2026-10-19: 219 offline tests passed in 34.211 seconds, no skips (`.tmp/event-box-patch-tests-all-retry.log`). After adding three cancellation-window regressions, the final 222-test suite completed in 34.565 seconds with 221 passing and one pre-existing `Config.save/os.replace` setup error (`WinError 5`); that individual test passed separately in 0.092 seconds. A complete rerun and then one no-retry, fresh-process-per-module run each had two errors of the same setup class; the module run passed 220 tests, including all 23 overlay and 25 local-camera tests, with no skips. Do not claim a fully passing final all-suite run. The same random replacement error occurred on the unmodified baseline and remains deferred; production configuration code was not changed to mask it. Final release checks: 9 passed in 0.161 seconds. Evidence: `validation-local/event-box-final-tests-*.log`, `event-box-module-*.log`, `event-box-module-report.json`. Both current and historical all-suite logs retain the pre-existing mock remote-output reader's background `queue.Empty` exception.
- Decode publication validation is bounded to 3 seconds: decode one first frame and consume the final 2-second region through EOF, with both streams producing frame hashes. Short clips decode from the beginning through EOF. Normally completed encoder output that fails this check retains its unchanged `.part.mp4` for recovery, hidden from completed-video consumers; this is not a full audit of a long video's middle.
- Annotation waits are bounded to 0.5 seconds with raw fallback, one outstanding native job per camera, no history queue and no late result across frames/events. A retiring job keeps its slot until it returns; shutdown interrupts waiting and later events can recover with a new detector. Cancellation is checked before entry, after acquiring the slot and immediately before native execution. Diagnostics are sanitized and bounded; logger errors do not disable raw fallback.
- Final Windows EXE: SHA-256 `33d436170ade537040c2e7a2e4ed4a96e9d20bc9c9014c6de7ec406f881af020`, 135,541,016 bytes. Six startup and ten fake-preview checks passed for this exact build. Its actual capture/detection/event/WeChat fake-CDN round trip produced 43 decoded red-box frames over 5.75 seconds; decrypted upload bytes matched the annotated MP4. Missing fields enabled red boxes and invalid settings were rejected atomically. Evidence: `validation-local/event-box-build.log`, `event-box-exe-startup.json`, `event-box-exe-preview.json`, `event-box-exe-report.json`.
- Activated this exact build at localhost:8810; existing login, receiving state, cameras and recording root preserved, remote relay not started, formal installation unchanged. Browser verified checked switch and red selection. Evidence: `validation-local/event-box-activation.json`, `event-box-settings.jpg`. Three independent reviews and focused patch reviews have no remaining actionable findings. Real-camera and real-phone annotated-video acceptance is pending and must bind to this hash; historical pre-review builds and earlier webcam acceptance are not reused. No GitHub publish performed.

- `.venv\Scripts\python.exe -m tests.run all`: complete existing and new offline suites; final execution has the known setup errors described above, no skips.
- `.venv\Scripts\python.exe -m unittest tests.test_windows_release`: release checks pass.
- `.venv\Scripts\python.exe build\build_exe.py`: stage candidate; verify actual EXE using synthetic media/fake WeChat before real-device/user checks.

## Suggested Review Order

**事件入口与帧配对**

- 事件开始时固定开关和颜色，选择编码路径。
  [camera.py:391](../../nvr/camera.py#L391)

- 检测决定保持原逻辑，触发帧与元数据成对交接。
  [hub.py:299](../../nvr/hub.py#L299)

**描边与检测隔离**

- 每个新帧独立检测，阴性清框且不修改原图。
  [event_overlay.py:146](../../nvr/event_overlay.py#L146)

- 真实确认框加回ROI偏移，保留原检测布尔结果。
  [detect.py:106](../../nvr/detect.py#L106)

**超时、取消和原画面回退**

- 单路唯一原生任务，超时回退并拒绝迟到结果。
  [event_overlay.py:32](../../nvr/event_overlay.py#L32)

- 共享JPEG录像保持设备单开，写入和退出均有界。
  [local_camera.py:97](../../nvr/local_camera.py#L97)

**完成文件发布**

- 完成校验后才发布MP4，失败保留待恢复文件。
  [camera.py:478](../../nvr/camera.py#L478)

- 限定时间内验证首帧和文件尾部至EOF。
  [ffmpeg.py:339](../../nvr/ffmpeg.py#L339)

**用户设置**

- 严格整单校验，错误设置不得部分写入。
  [server.py:436](../../nvr/server.py#L436)

- 中文开关与三色选择，说明清晰度和CPU代价。
  [index.html:239](../../web/index.html#L239)

**默认值和回归证据**

- 旧配置缺字段时仍默认开启红色框。
  [config.py:142](../../nvr/config.py#L142)

- 取消窗口不启动多余原生任务。
  [test_event_overlay.py:194](../../tests/test_event_overlay.py#L194)

- 实际解码移动框和阴性清框。
  [test_event_overlay.py:373](../../tests/test_event_overlay.py#L373)

- 首事件红色，下一事件采用新颜色。
  [test_event_overlay.py:477](../../tests/test_event_overlay.py#L477)

- 微信取回的带框录像字节一致。
  [test_clawbot_events.py:615](../../tests/test_clawbot_events.py#L615)
