---
title: Real-time selected-object preview overlays
type: feature
created: 2026-10-20
status: done
baseline_commit: f58689554606ded2b99dfc83a2ae9bb20d305be5
context: [docs/ALGORITHM_STORE_QUICK_START.md]
---

<frozen-after-approval reason="User explicitly requested this feature on 2026-10-20; implementation within that request is authorized">

## Intent

**Problem:** V3 shows class-colored boxes and Chinese labels only in completed event videos. Users need to see the applied algorithm's selected objects while watching camera preview, with continuous motion rather than stalls or detached boxes.

**Approach:** Add a shared, asynchronous preview-only renderer, using the existing applied store model's exact semantic results and image-supported tracking between them. The default desktop preview shows boxes with an independent raw-view switch. The camera-scoped WeChat preview uses the same cached visual result, within its existing refresh rate and authorization.

## Boundaries & Constraints

**Always:** Preserve capture, raw JPEG, timelapse, event decisions, recording, playback and WeChat event-query semantics. Reuse the applied NanoDet/YOLOX model and selected classes, class palette and licensed Chinese font. Never add inference per viewer. Overlay is eligible when store mode and global/per-camera detection are enabled; other modes keep existing raw preview and the UI explains how to enable selected-object preview. No implicit algorithm activation. Bound retained frames, worker count and cache age; slow inference/rendering falls back to current raw pixels instead of freezing preview or accumulating jobs. Report performance and real-scene acceptance separately.

**Ask First:** New model/library downloads beyond existing configured models, cloud inference/private-image upload, new remote exposure, changing event triggers or recognition classes, and GitHub publication.

**Never:** Repaint last_jpeg, feed painted frames into detection/recording, reuse old coordinates without current-frame tracking, refresh semantic lifetime from optical flow alone, or promise zero latency and perfect tracking on arbitrary hardware. No historical artifact receives a new acceptance claim.

## I/O & Edge-Case Matrix

| Input/state | Expected result |
|---|---|
| Applied store model, enabled camera/detection, fresh result | Colored class outlines and Chinese labels on matching current preview pixels |
| Slow result from an earlier retained raw frame | Consume on its exact source frame, then track through ordered raw frames to current; no coordinate relabeling |
| Brief missed detection, supported image motion | Bounded continuity using existing tracker; semantic lifetime remains tied to model confirmation |
| Expired/invalid/source-epoch/model/target/size change | Clear incompatible state and show raw until a valid fresh result arrives |
| Detection disabled, old mode, raw-view switch | Raw preview; event-box preference stays unchanged |
| Multiple viewers or repeated same-frame requests | Read one cached result; no new native work or frame queue per viewer |
| Worker failure, stalled input, shutdown | Timely raw response, bounded retirement, no new worker while an old one is running |

</frozen-after-approval>

## Code Map

- `nvr/live_overlay.py`: pure paired-frame overlay/tracking history and one lazy per-camera preview worker.
- `nvr/camera.py`: preview-only snapshot/cache lifecycle; raw snapshot remains unchanged.
- `nvr/algorithm_runtime.py`, `nvr/target_tracking.py`, `nvr/event_labels.py`: reuse exact inference result contracts and existing tracker/renderer without a second model.
- `nvr/hub.py`: make the shared market-detection interval count from each round's start, with bounded overrun waiting; preserve legacy cadence and event conditions.
- `nvr/server.py`, `nvr/preview.py`: desktop and authorized camera-scoped preview read shared cache outside capture locks.
- `web/index.html`, `web/app.js`: independent preview checkbox and explanatory state without rebuilding the wall on normal status updates.
- `tests/test_live_overlay.py`, `tests/test_api.py`, `tests/test_preview.py`: temporal, concurrency, HTTP, raw-preservation and failure regressions.
- `build/build_exe.py`, local validation scripts: exact Windows resources and isolated executable verification.

## Tasks & Acceptance

**Execution:**
- [x] Add bounded paired-frame replay and tracker snapshots so delayed results are applied on their original frame, not incorrectly marked as fresh.
- [x] Add a single lazy worker/cache per camera, bounded age and shutdown; reuse existing semantic inference only.
- [x] Integrate desktop and capability routes, replacing stale camera references safely; retain authorization and raw access.
- [x] Add the default-on desktop preview switch, clear store/detection guidance and warning display.
- [x] Test tracking motion, jitter, miss/exit, class labels, revision/epoch invalidation, multiple viewers, slow workers and raw media preservation.
- [x] Run meaningful regression, native public-fixture/performance checks and isolated Windows build; leave current published EXE/data intact.
- [x] Cover bounded Windows atomic-save recovery, preserving the prior complete configuration on persistent sharing failure.
- [x] Correct shared market cadence and test previews with event annotation disabled, keeping event thresholds and legacy scheduling intact.
- [x] Refresh shared semantics only during active preview demand, independently of configured event decisions; retain one model/native slot and bound shutdown.

**Acceptance Criteria:**
- Given a textured translating target with paired semantic results, when interleaved frames are previewed, then at least 95% of measured centers remain within 6.1 pixels (six-pixel target with 0.1-pixel numerical tolerance) and target disappearance clears stale boxes within the existing 0.75-second semantic bound.
- Given an 8 fps representative local fixture, when preview processing runs, then capture/read APIs remain nonblocking, rendered throughput approaches input throughput and processing latency is measured; slow native work cannot queue frames or multiply workers.
- Given one versus three viewers, when the same camera is requested, then inference/model count and render work are independent of viewer count.
- Given model/target, source/epoch or size changes, when requests continue, then an earlier generation's overlay is never returned for the new context.
- Given preview disabled or failed, when event recording/timelapse executes, then its input bytes and event decision lifecycle remain unchanged.
- Given a Windows candidate, when launched with isolated data, then embedded overlay/font/model assets, previews and shutdown pass; actual camera smoothness remains a separate user acceptance step.

## Spec Change Log

- 2026-10-20: New human instruction supersedes V3's earlier raw-preview-only scope for the preview display only. Existing raw capture, timelapse and event safety constraints remain. The explicit feature request authorizes development without a second routine permission prompt; publication remains separate.
- 2026-10-20: Independent review found opened streams surviving password rotation and a stop-versus-first-worker race. Bind stream authorization generation and serialize lifecycle admission; keep cache-only HTTP and original media inputs. Full regressions also repeatedly exposed an existing Windows atomic-config replacement conflict in unrelated test setup. An isolated 600-replace control reproduced five WinError 5 failures, all succeeding after 20 ms. Limit recovery to Windows errors 5/32, four atomic attempts with at most 140 ms backoff; preserve the original on permanent failure and propagate other errors. No retry is added to capture, inference or event timing.
- 2026-10-20: Native public-fixture checks exposed semantic gaps after event annotation retired on its existing timeout: Hub slept the full configured interval after inference, so preview depended on event annotation for enough fresh results. Correct the shared market round cadence to count from round start, with a small interruptible overrun wait. Keep the applied model, confidence, consecutive-hit/leave/cooldown conditions, annotation timeout/retirement, legacy-mode cadence, original frame timestamps and semantic lifetime. Controlled native-thread caps of 2/4/8 worsened inference, so do not add one. Add checks with event annotation disabled; do not hide missing results with longer-lived coordinates.
- 2026-10-20: Exact r2 EXE passed one/three-viewer event-fixture checks at 7.983–8.019 fps and 100% measured coverage, but the default 0.5-second/no-event check exposed two brief raw frames (55/57 marked, below the unchanged 98% gate). Preserve this failure. At a normal semantic-renewal boundary, the previous paired frame can exhaust its 0.30-second display budget just before the new result arrives. Add one Hub-level, viewer-demand-scoped semantic coordinator using the existing shared runner/native slot, with start-to-start refresh and no queued inference. Keep configured event decisions in the original path; extra semantic refresh never updates hit/person/start/stop state. Keep the 0.75-second semantic bound, 0.30-second display bound, model/targets, identity guards, original capture bytes and all prior lifecycle fixes.

## Design Notes

The existing model runner returns an immutable batch with exact source epoch, sequence, timestamp, model revision and JPEG digest. Preview retains a small bounded history of pristine packets and tracker states. Late detections rewind to the state preceding their original packet and replay ordered current-image motion, preserving original confirmation time. HTTP threads only request/read a cache; they never run OpenCV. A timed-out native worker retains its slot until it returns.

One Hub-level semantic coordinator services active preview leases through the same per-camera runner and global native slot. It produces exact paired results without entering the event-decision path. No viewer creates a model or inference thread; no demand means no extra semantic requests. The existing event loop retains its configured schedule and decisions. Shared CPU/native capacity remains finite, so overload still returns current raw pixels with a warning.

Coordinator admission never queues behind the algorithm lock; after admission it holds that lock for the existing bounded inference call (up to 0.5 seconds per admitted camera). This protects model apply/reset from an old queued revision. Shared native/CPU contention can delay actual event or configuration execution under load; configured thresholds, confirmation counts, leave conditions and event-decision entry points are unchanged. No hard real-time event guarantee is introduced.

## Verification

Use the repository's Python 3.12.10 environment with TEMP/TMP in `.tmp`. Run live-overlay tests first, then relevant algorithm/tracking/API/preview/media/WeChat regressions and the full offline suite. Record synthetic throughput, source identity, exact build hash, isolated executable checks and any user camera acceptance separately in `validation-local` and the non-frozen progress section.

### Progress evidence (2026-10-20)

- The 58 focused overlay, route, cadence and atomic-save checks passed. The complete offline suite passed 360/360 in 52.106 seconds (`validation-local/live-preview-full-cadence-final.log`). JavaScript syntax and diff whitespace checks passed.
- Source native public-fixture check: `validation-local/live-preview-deferred-first-native-report.json` PASS. One viewer 8.019 fps; three viewers 7.964–7.981 fps at 8 fps input. All steady-state received HTTP frames retained both labels (100%), with no raw-JPEG insertion or sequence reversal; lag at most one frame. Raw snapshots/timelapse stayed unpainted and completed events matched HTTP playback.
- Default 0.5-second interval with event annotation disabled and no event writer: `validation-local/live-preview-default-no-event-deferred-source-report.json` PASS. This verifies the shared Hub supplies preview semantics independently of recording.
- Measurement method: retain every received JPEG and its arrival time, then decode/check every frame after all streams close. The original FPS, 98% coverage, lag and order gates are unchanged. Earlier online full-image color/difference observers materially competed with inference; all failed runs remain preserved, and they are not relabeled as passes. This does not establish performance under arbitrary CPU load.
- Exact returned public HTTP JPEG visually inspected: category outlines, Chinese labels, colors and placement are readable. Synthetic translation/departure checks are distinct from model accuracy. Actual camera and phone smoothness remain pending user acceptance; past V3 event acceptance is not preview acceptance.
- Isolated Windows 3.1.0 candidate built: 139182861 bytes, SHA-256 `9e6d8cc377c0ac73064a796c9a30968c72a9211c7f0287d6699c91d0dd116e93`. Embedded overlay module, exact web assets, font/catalog/model assets validated; prior accepted executables remain intact. Exact-EXE live/no-event checks are running during independent code review and must pass before local activation. No GitHub publication is authorized by this feature request.


### Final reviewed candidate (2026-10-20)

- All review corrections are closed. Three independent follow-up reviews (blind product diff, edge paths, and frozen-intent acceptance) found no remaining confirmed blocking issue. Immutable reviewed full diff SHA-256: `acfdf1482d447b6e00b88ad4516105a6c0867cf8feb4bc501f3de43e55a052b8`. Final documentation receipts are subsequent to that runtime review.
- The final complete offline suite passed **410/410**, no skips, in 61.472 seconds (`validation-local/live-preview-full-r4-final.log`). JavaScript syntax and diff whitespace checks passed.
- Final isolated Windows candidate: **139190677 bytes**, SHA-256 `89dd6e1b39cf448e5ba88a1d14088ba29577fe9b127662620c0c089f6f33f938`. Source/resource hashes match both final executable reports and the build receipt; the accepted V3.0 executable remains intact.
- Exact final EXE one/three-viewer native check PASS (`validation-local/live-preview-exe-r4-phase-bounded-report.json`): steady unique output **7.946–7.980 fps** from an 8 fps fixture, minimum measured class coverage **100.0%**, no frame reversal, unchanged 98% coverage gate. Every received JPEG was retained and analyzed, with no sampling or observer-time visual work. Raw snapshots/timelapse remained unpainted; completed event bytes matched HTTP playback.
- Default 0.5-second cadence without recording PASS (`validation-local/live-preview-default-no-event-deferred-exe-r4-report.json`): **8.036 fps**, **100.0%** measured coverage, no event writer/MP4. This is separate from event-driven inference.
- An earlier r2 attempt hit the original toggle-to-drain deadline after a roughly 14-second file startup; the failed report is preserved. The reviewed observer now has a separate bounded 30-second first-online phase and starts the original 26-second drain window once. All steady-state FPS/coverage/order/lag checks remain unchanged; no product change or unverified cause is attributed to this startup delay.
- Synthetic paired translation/departure check PASS: 68 frames, renderer p95 17.170 ms; the six-pixel-plus-0.1 numerical tolerance and original semantic expiry are unchanged. Public-photo motion is not evidence of arbitrary real-object tracking accuracy.
- The exact final-EXE public HTTP JPEG was visually inspected: readable Chinese “狗”/“汽车” labels, correct yellow-green/orange outlines and matching target positions. The original returned JPEG was not redrawn or re-encoded.
- Implementation/build verification is complete; real-camera preview smoothness and phone preview are **pending user acceptance**. Spec `done` refers to the development deliverable. Local activation uses a cloned trial directory; original executable/config/recordings are preserved. No GitHub push or Release publication is part of this request.

## Suggested Review Order

**预览入口与共享缓存**

- 预览只读共享结果，保持原始画面独立。
  [camera.py:587](../../nvr/camera.py#L587)

- 单路后台绘制，缓存过期及时回退。
  [live_overlay.py:314](../../nvr/live_overlay.py#L314)

- 检测结果对应原帧，再跟踪到当前画面。
  [live_overlay.py:147](../../nvr/live_overlay.py#L147)

**权限与生命周期**

- 根据当前保存的摄像头设置决定是否画框。
  [camera.py:564](../../nvr/camera.py#L564)

- 重查摄像头、模型和授权，拒绝旧结果。
  [server.py:252](../../nvr/server.py#L252)

- 微信预览核对画框前后的识别条件。
  [preview.py:347](../../nvr/preview.py#L347)

- 跨摄像头重建保留退出槽，避免并发积压。
  [live_overlay.py:385](../../nvr/live_overlay.py#L385)

**识别供给与界面**

- 共享补充识别结果，不进入事件判定。
  [hub.py:307](../../nvr/hub.py#L307)

- 复用有效正负结果，跳过在途模型任务。
  [camera.py:683](../../nvr/camera.py#L683)

- 共享事件检测按轮次起点调度。
  [hub.py:350](../../nvr/hub.py#L350)

- 预览开关独立切换，不改变录像设置。
  [app.js:119](../../web/app.js#L119)

- 默认开启彩框与标签。
  [index.html:119](../../web/index.html#L119)

**回归与支持**

- 验证共享刷新不改变事件状态。
  [test_preview_coordinator.py:46](../../tests/test_preview_coordinator.py#L46)

- 验证新鲜空结果与在途模型复用。
  [test_preview_semantics.py:158](../../tests/test_preview_semantics.py#L158)

- 覆盖连续跟随与处理退化后恢复。
  [test_live_overlay.py:438](../../tests/test_live_overlay.py#L438)

- 覆盖微信模型切换与检测关闭。
  [test_live_preview_routes.py:571](../../tests/test_live_preview_routes.py#L571)

- 验证共享检测与原有事件条件。
  [test_detection_cadence.py:127](../../tests/test_detection_cadence.py#L127)

- 仅重试 Windows 临时原子保存冲突。
  [config.py:167](../../nvr/config.py#L167)

- 验证保存失败时旧配置仍完整。
  [test_config_persistence.py:31](../../tests/test_config_persistence.py#L31)

- 提供直接试用和微信预览说明。
  [LIVE_PREVIEW_DETECTION.md:5](../LIVE_PREVIEW_DETECTION.md#L5)
