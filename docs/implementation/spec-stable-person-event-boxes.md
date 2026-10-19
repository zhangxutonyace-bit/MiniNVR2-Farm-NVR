---
title: '事件录像的人物框连续跟随'
type: 'bugfix'
created: '2026-10-19'
status: 'done'
baseline_commit: 'dc623bed399ee51629b1b7c382f6788124fb344a'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Real computer/WeChat playback has boxes, but the user's clip shows jumps, wall misdetections and visible people missed. Smoothing wrong detections cannot solve following quality.

**Approach:** Combine offline person localization with image-based tracking: follow movement, smooth small corrections and clear unsupported tracks. Preserve the default-on switch, three colors and video delivery.

## Boundaries & Constraints

**Always:** Preserve Hub/`Detector.process` event decisions and timing. Annotation owns independent state; capture remains single-open. Auto/Haar modes get person annotation; motion mode remains motion regions. Preserve preview/timelapse pixels, per-event settings, quality/CPU notice, bounded worker and 0.5-second raw-fallback budget.

Bundle OpenCV Zoo NanoDet (3,800,954 bytes), SHA-256 `4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186`, person class only, existing OpenCV DNN/CPU, origin/Apache-2.0 notices. No runtime downloads/cloud inference. Approval authorizes this annotation-only model and continuous tracking under the previous story's ask-first restriction.

**Ask First:** Different model, GPU/heavy runtime, paid/cloud service, changed event rules or mandatory recording-quality change.

**Never:** Treat contours as confirmed people; copy unsupported coordinates; claim perfect tracking/identity; retrofit videos, publish GitHub, expose access or upload private recordings.

## I/O & Edge-Case Matrix

| State | Behavior / failure handling |
|---|---|
| Movement | Apply displacement immediately; smooth correction residuals/size |
| Brief miss | Require current image evidence; expire 0.75s after last person confirmation; tracking failure clears immediately |
| New/fast target | Reliable detection may reinitialize at zero IoU; no duplicated assignment |
| Departure/occlusion | Clear unsupported boxes; flow cannot prolong confirmation expiry |
| Duplicate/discontinuity | Duplicate cache is idempotent; source/size change, reorder, excessive gap or empty input resets |
| Model/native failure or timeout | Raw video plus warning, no contour fallback/backlog/shutdown blockage |

</frozen-after-approval>

## Code Map

- `nvr/event_overlay.py`: private annotation/cache and bounded runner.
- `nvr/detect.py`, `nvr/hub.py`: protected event baseline.
- `nvr/camera.py`, `nvr/local_camera.py`: encoding/publication.
- `build/build_exe.py`, `scripts/package_windows_release.py`: executable/bundle assets.

## Tasks & Acceptance

**Execution:**
- [x] `models/`, `third_party/`, `docs/THIRD_PARTY_SOURCES.md`: pin official model/digest/license.
- [x] `nvr/person_localizer.py` (new): bounded inference/proposals, aspect-ratio mapping, finite/clipped person boxes and explicit failures.
- [x] `nvr/target_tracking.py` (new): bounded tracks/features, forward/backward flow, association, residual smoothing, expiry/reset.
- [x] `nvr/event_overlay.py`: integrate pristine frames without contour-seeded people; preserve motion provenance/cache/fallback.
- [x] `tests/test_target_tracking.py`, `tests/test_event_overlay.py`, `tests/run.py`: labeled movement/background, jitter/misses, departure, rapid jumps, wrong detections, two targets, stationary targets, discontinuities/failures; decode moving MP4s.
- [x] `build/build_exe.py`, `scripts/package_windows_release.py`, `tests/test_windows_release.py`: bundle model/notices; verify offline assets.
- [x] `README.md`, `docs/WECHAT_QUICK_START.txt`, `docs/VALIDATION.md`: behavior/limits and precise acceptance evidence.
- [x] `validation-local/`: baseline comparison, CPU measurements, local clip inspection, exact-hash EXE/loopback WeChat checks; prepare configuration-preserving activation and fresh real-user check workflow. Execute activation after independent review; record real observations separately when available.

**Acceptance Criteria:**
- Given labeled 480x270/8fps textured targets moving 8 or 20px/frame with two missed confirmations, when tracking runs, then decoded center error stays within 6px on at least 95% of supported frames, without accumulated lag; unsupported boxes expire within 0.75s.
- Given +/-6px detection jitter/departure/unrelated detections/two targets, when association runs, then stationary center jitter decreases by at least 25%, with no ghosts, duplicated assignments or unrelated interpolation; reliable fast-jump detections relocate immediately.
- Given baseline inputs/annotation failure, when enabled, then event decisions/timing remain equivalent and work/cancellation stay bounded.
- Given the rebuilt EXE, when WeChat retrieves an event, then mock-upload bytes match MP4; real following acceptance binds separately to its hash.

## Spec Change Log

- 2026-10-19: User accepted playback but rejected jumping/non-following boxes; annotation-only localization/tracking proposed.
- 2026-10-19: User approved the plan. Clarified execution ordering outside frozen intent: independent build/mock verification precedes review; local activation and fresh human following checks follow review. Human results remain separate and are never inferred from old acceptance or synthetic tests.
- 2026-10-19: After confirming fresh-event following and computer/WeChat playback, the user explicitly authorized publishing this accepted version to GitHub with an updated Windows package and Release notes. This later human instruction supersedes the earlier local-only publication boundary; frozen implementation intent is retained as history. Distribution keeps the same accepted EXE and separate evidence scopes.

## Design Notes

Validate interior LK features forward/backward; cap work without a history queue. Measure CPU against the worker budget. The supplied clip contains burned-in borders/compression and supports diagnosis, not unbiased recognition ground truth.

Sources: [OpenCV Zoo NanoDet](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_nanodet), [OpenCV optical flow](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html).

## Verification

- Pre-review source: 240 offline tests passed in 43.197s, no skips; 10 release checks passed in 0.424s. Initial failures and existing background mock-reader errors remain recorded in [validation details](../VALIDATION.md), not erased.
- Historical pre-review EXE: SHA-256 `b565a7a8e69cb0385201f93aee2c66ee27f8766091a7b9a7fa9b5172f42bf3bf`, 139,093,411 bytes. Exact model/notices/modules, six startup and ten preview checks passed. Motion synthetic and compiled auto-person annotation both generated decodable MP4s and identical decrypted loopback WeChat returns. Person-path input is a supplied already-boxed local clip, not fresh device acceptance. This EXE was not activated; the replacement and fresh receipts are listed below.
- Final post-review source: 47 focused tests passed in 17.210s; 10 release checks passed in 0.411s. The full 245-test run had two existing Config.save/WinError 5 setup errors, both standalone rechecks passed; the full retry had one such setup error (244 other tests passed, no failures/skips), and its standalone recheck passed in 0.008s. This is not a fully green final combined run; the unchanged production configuration logic and every original log are retained.
- Three review patches are closed by focused blind/edge reviews: confirmed detections take capacity priority, large size changes adopt immediately, near-concentric contained old/new geometry does not create a duplicate while independently supported targets remain distinct. Five new regressions fail against the prior code and pass after patching.
- Final EXE: SHA-256 `f2048518ef81595bad6eaac61884ded4fe1eda9ad6a5d241fd65abf09dfd7090`, 139,093,328 bytes. Assets, six startup and ten preview checks passed on this exact file. Motion MP4:42 frames/5.625s; auto-person MP4:82 frames/10.625s,57 frames with new blue boxes and no annotation warnings. Both completed MP4s matched decrypted loopback WeChat upload bytes. The latter uses the already-boxed private clip locally, not fresh camera or real-phone acceptance.
- Final product-path sample: first/maximum annotation render91.852ms (p50 62.006ms,p95 64.198ms), no warnings; 106 supplied frames,88 new-box outputs, not accuracy. Event Detector/Hub sources equal baseline. Local receipts: `validation-local/tracking-*.json` and logs.
- Final acceptance audit found no actionable issue and independently verified exact EXE/source/module/model/receipt bindings. The final EXE is now running locally at 8810; cameras, recording root and WeChat login/receiving state are preserved. Boxes are enabled/red, remote relay remains stopped and the formal AppData installation is unchanged. UI evidence: `validation-local/tracking-settings.jpg`; activation: `tracking-activation.json`. On 2026-10-19 the user explicitly confirmed the fresh-video check: “框能跟随，电脑和微信播放正常”. The separate `tracking-user-acceptance.json` binds this result to the final EXE hash and implementation commit. Acceptance covers this computer/camera/WeChat account and new event; other scenes, occlusion, night conditions and multi-camera load remain unverified. No GitHub publication or remote exposure.

- `python -m tests.run all`; `python -m unittest tests.test_windows_release`: distinguish existing Windows fixture errors.
- Compare baseline decisions; measure decoded localization/continuity/lag/expiry against synthetic labels.
- Exact-hash EXE: offline assets/startup, synthetic annotation and fake WeChat round trip.
- Fresh real event: walk, pause, reverse, leave; inspect computer/WeChat. Partial/occluded/tiny people remain limitations.

## Suggested Review Order

**标注入口与原事件边界**

- 自动/Haar标注走独立人物定位；移动模式保留移动区域。
  [event_overlay.py:153](../../nvr/event_overlay.py#L153)

**人物定位与连续跟随**

- 固定离线模型，仅输出人物候选，不参与事件触发。
  [person_localizer.py:69](../../nvr/person_localizer.py#L69)

- 前后向图像检查拒绝背景支撑；短漏检不能延长人物确认。
  [target_tracking.py:89](../../nvr/target_tracking.py#L89)

- 位移立即应用，可靠大变化重定位，当前确认优先占用容量。
  [target_tracking.py:135](../../nvr/target_tracking.py#L135)

- 近同心几何消除旧新双框，同时保留独立目标。
  [target_tracking.py:24](../../nvr/target_tracking.py#L24)

**离线模型与分发完整性**

- 构建前验证固定摘要，并内置模型和许可证。
  [build_exe.py:74](../../build/build_exe.py#L74)

- 分发检查实际EXE内的模型与许可字节。
  [package_windows_release.py:43](../../scripts/package_windows_release.py#L43)

**回归与交付证据**

- 编码再解码验证移动、漏检和画框中心，保留时间预算。
  [test_event_overlay.py:312](../../tests/test_event_overlay.py#L312)

- 覆盖容量、尺寸变化、同心重复与独立短漏检反例。
  [test_target_tracking.py:122](../../tests/test_target_tracking.py#L122)

- 模型和许可缺失或篡改必须拒绝分发。
  [test_windows_release.py:300](../../tests/test_windows_release.py#L300)

- 区分程序检查、已知测试准备错误和真实跟随验收。
  [VALIDATION.md:13](../VALIDATION.md#L13)
