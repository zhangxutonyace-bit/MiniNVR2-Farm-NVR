---
title: 'V3.0 算法商城与多目标事件录像'
type: 'feature'
created: '2026-10-19'
status: 'done'
baseline_commit: 'f43f9d39ce0900e35bf9671c07e47441a04f20d7'
context: []
---

<frozen-after-approval reason="human-owned intent — preserve the user's confirmed scope">

## Intent

**Problem:** V2.4 supports person events only. Users need downloadable detection models and checkbox-selected household targets driving the existing recording workflow.

**Approach:** Add an algorithm-store tab with curated, pinned ONNX models: bundled NanoDet COCO, downloadable YOLOX Nano and YOLOX-S. One active model handles all checked capabilities in a single forward. Selected semantic detections trigger existing event confirmation, gone, cooldown and maximum-duration rules; completed MP4s retain playback/download/WeChat behavior.

User confirmed these names: person, car, stool, chair, table; cat, dog, pets, phone, sofa, bed, TV, laptop, cup, bottle. Stool/chair explicitly merge into one “座椅（椅子/凳子）” switch with no separate-class claim. Pets expands to cat+dog; overlapping selections deduplicate. Table uses the dining-table class, with coverage limits disclosed. Default other targets remain unchecked; stationary selected objects can sustain events, explained beside selection.

User subsequently requested Figma-based different-color boxes and YOLO-style target labels. For store-mode event videos, assign stable per-class colors from the FigJam single-model/multiple-label palette: red #F51616, orange #E86D08, dark blue #0931C0 and yellow-green #91C20D. Show the category color beside its store checkbox, and burn the actual Chinese class name (行人、座椅、猫、狗, etc.) into a compact color-backed label attached to its outline. Reuse this four-color palette across the thirteen supported COCO classes; colors distinguish categories and do not claim severity, identity or tracking IDs. Pets displays the actual cat/dog class once. Keep complete labels within the frame, use readable foreground contrast, and include a small licensed offline Chinese font rather than assuming a system font exists.

## Boundaries & Constraints

**Always:** Preserve legacy auto/Haar/motion behavior until the user applies store settings. Keep existing capture single-open and raw preview/timelapse pixels untouched. Downloading does not activate. The active model is one catalog choice; targets are checkbox capabilities. Keep model data under persistent data/models, not onefile temporary extraction. Show official source/license, size, progress, cancel and actionable errors.

Catalog contains exact official asset identity, size/SHA, adapter profile and class map. Only catalog IDs are accepted: HTTPS and bounded approved redirects, timeout/size/deadline, private temporary download, digest and native compatibility checks, then atomic install. Do not execute downloaded Python, pickle or PT files. Invalid/cancelled installs leave the previous model usable. No runtime download before an explicit download action.

Share frame-matched typed results across event decisions/annotation. A camera/source/sequence/model-revision key prevents duplicate forwards and stale coordinates. Bounded native work, no frame history backlog or unlimited replacement jobs. Tracking is class-aware, with a total per-camera cap, current-image evidence and existing short expiry. Flow-only support never increments confirmation or refreshes semantic “last seen”. Keep the 0.5-second annotation wait/raw fallback and visible warnings.

Activation validates the entire selection and candidate model before saving; failure preserves config/runtime. Applying changed model/targets closes the current event cleanly before the new revision; clips are retained and never mix algorithm settings. Global detection disable remains immediate. Settings, cameras, credentials, recordings and V2.4 executable stay intact during development.

The existing box switch still controls event annotation. Legacy modes retain their existing global color behavior. Store-mode category outlines have no target-area fill; only the compact label has a background. Labels never modify raw preview/timelapse pixels, add native detection work or retroactively change old recordings. Playback, download and WeChat share the same finished labeled MP4. Font/render failure retains raw media and a visible warning.

**Ask First:** Paid service, cloud inference or private-image upload, heavy new runtime/GPU requirement, arbitrary/custom plugin loading, or additional recognition goals.

**Never:** Pretend stool is a distinct COCO class; describe cat/dog grouping as universal pet recognition; claim scene accuracy from synthetic tests; infer phone or V3 acceptance from V2 receipts; publish GitHub or expose remote access without a new publication request.

## I/O & Edge-Case Matrix

| State | Expected result |
|---|---|
| Download/cancel/hash or compatibility failure | Bounded job/status; no partial install or selection change |
| Restart/corrupt or missing installed model | Persistent valid install or visible unavailable state; no silent class fallback |
| Apply unsupported/empty targets, invalid model/threshold | Atomic rejection; old config and runtime remain |
| Cat+pet, overlapping person+phone | Deduplicated pet classes; independent typed boxes, class-specific NMS |
| Fresh positive/negative, flow-only, blocked inference | Only fresh semantic decisions affect events; raw media and bounded shutdown survive failures |
| Old frame/source/config revision after switch | Reject stale result, reset tracking/confirmation, preserve completed old clip |

</frozen-after-approval>

## Code Map

- `nvr/algorithm_catalog.py`, `model_store.py`, `object_models.py`, `algorithm_types.py`: catalog, persistence, adapters, immutable contracts.
- `nvr/algorithm_runtime.py`, `camera.py`, `hub.py`, `event_overlay.py`, `target_tracking.py`: bounded shared inference, semantic decisions, class-aware video annotation.
- `nvr/config.py`, `server.py`, `web/index.html`, `web/app.js`: migration, protected APIs and Chinese store UI.
- Event label renderer and bundled font/notices: stable category colors, readable Chinese labels and bounded frame-edge placement.
- `nvr/ffmpeg.py`: bounded publication validation must actually consume the last two seconds through EOF, rather than stopping its second input after one frame.
- `build/build_exe.py`, `scripts/package_windows_release.py`: offline resources, notices and exact-artifact verification.

## Tasks & Acceptance

- [x] Pin official models/profiles/license; verify real OpenCV forward and public non-private fixture against reference profiles.
- [x] Implement catalog-ID-only transactional model install/cancel/status and restart integrity.
- [x] Implement typed multi-class adapters and deduplicated target expansion, single-forward filtering.
- [x] Integrate bounded shared runtime, fresh-decision event state, class-aware tracking and immutable source/revision checks.
- [x] Add protected store/status/download/cancel/apply APIs, transactional config, Chinese model/checkbox UI and truthful capability notices.
- [x] Add Figma four-color per-class event outlines, Chinese YOLO-style labels, store color swatches and licensed offline font; verify frame edges, raw fallback and exact MP4/WeChat reuse.
- [x] Cover legacy migration, failed installs/activation, concurrency/staleness/retiring work, tensor geometry, class overlap and decoded event MP4 plus fake WeChat byte integrity.
- [x] Repair the exposed tail-validation coverage gap with head-one-frame plus tail-through-EOF checks sharing the existing total 3-second deadline; reject late coded-sample corruption without changing recording codec or adding retries.
- [x] Build an isolated V3 Windows candidate; check embedded resources, download/apply/restart and media pipeline; request fresh user acceptance before any release.

Given a verified installed model and checked targets, applying starts semantic detection only for those classes, with the existing event lifecycle. Given invalid inputs or unavailable models, no partial configuration becomes active. Given a fresh event, computer playback and fake WeChat return the same completed annotated MP4. Given V2 configuration, legacy behavior remains until explicit application.

## Spec Change Log

- 2026-10-19: User requested V3 development; explicitly confirmed the 15 names, selected-target automatic recording with existing playback/WeChat, and merged stool/chair presentation. These confirmations authorize this implementation scope. Model/profile choices and architecture are routine implementation details within that scope; no additional permission is required.
- 2026-10-19: User explicitly extended the approved intent: “使用不同颜色的识别框” and labels naming “行人、椅子、猫、狗等”, following the earlier FigJam link. Amended only the frozen sections needed to capture this new human instruction; retained all previous recognition, event, privacy and publication boundaries. Public FigJam canvas was re-read after the connector required unavailable edit access. Its single-model/multiple-label palette is #F51616/#E86D08/#0931C0/#91C20D; no class-to-severity mapping is implied. Earlier approved frozen SHA remains recorded in validation-local/v3-approved-intent.json; the extended scope has a separate revision receipt. Existing V3 tests and native samples precede this label change and do not prove the new renderer.
- 2026-10-19: Independent real-model source flow exposed a publication-validation concern. First dog event was retained as pending validation; its media was not captured, so its root cause remains unknown. Later exact source dog flow produced correctly annotated media with byte-identical fake WeChat delivery. Separate reproducible diagnostics proved the legacy dual-input framehash command checked only one tail frame and accepted last/penultimate coded-sample corruption. Add the necessary bounded tail-validation repair and regression to execution scope. KEEP: successful encoder exit, unchanged 3-second total validation deadline, pristine original media, atomic publication, retained pending files on validation failure, all legacy and selected-class event rules; never add retries or call the original unknown failure explained merely because this independent defect is fixed.

## Verification

Offline tests use fake downloads/native results and synthetic labels; real graph checks use official public fixtures only. Preserve complete failure logs and distinguish existing Config.save/WinError 5 test setup errors. Independently review implementation and exact-hash compiled candidate. Fresh physical/WeChat acceptance remains separate and pending until the user reports it.

Before the new label change: 276 functional and 18 packaging tests passed. Public NanoDet at confidence 0.5 did not meet the dog/car positive fixture requirement; this is recorded as unsuitable, not a passed application flow. Its measured public-image scores support a separate explicitly labeled default-0.35 test, still pending. YOLOX Nano source person-only negative passed; initial dog finalization failed and retained no captured media. Retry1 produced a valid dog MP4 but the helper disabled its camera before fake WeChat queries, which only include enabled cameras. With test order corrected (no threshold/deadline change), retry2 dog completed 56 frames, 44 correctly annotated frames, zero car-region overlay, unchanged raw JPEGs, persistent model restart and exact fake WeChat MP4 byte equality. These receipts predate per-class Chinese labels and are historical development evidence, not final-build or phone acceptance. See ignored `validation-local/v3-source-*`, `v3-independent-graph-nanodet-coco-report.json` and `v3-media-diagnosis/tail-integrity/report.json` for complete evidence.


## First Candidate Verification (2026-10-19, historical after review)

- Source freeze: 285 functional tests passed, plus 18 packaging checks; targeted labels/media tests passed. Python compile, JavaScript syntax, catalog/font pins and diff checks passed.
- Exact isolated Windows candidate: version 3.0.0, 139164023 bytes, SHA256 `ba5e92cb56c87b95d1b02247faf7bffee72529ccd297e80b6c0751a077272f75`. Embedded web, model, Chinese label font, catalog, notices and required modules matched source. Six startup, ten preview and four simulated-event checks passed.
- Independent final compiled flows passed ten public-fixture cases: YOLOX Nano 0.5 person/dog/car/both; bundled NanoDet default 0.35 person/dog/car/both; YOLOX-S 0.5 person/both. Both light-model dog flows verified fake WeChat encrypted upload decrypts to the exact completed MP4; persistence and old-clip reuse survived restart. These cache-preseeded checks are separate from online-download proof.
- Fresh empty compiled app independently downloaded the exact 3,659,407-byte YOLOX Nano from its official HTTPS asset, verified its pinned digest, left active settings unchanged during download, applied the model and preserved it across restart. No camera/account was configured for that test.
- Root visual inspection of decoded final-EXE public frames confirmed readable Chinese car/dog tags and matching orange/yellow-green outline boxes for all three models. Synthetic thirteen-class layout and actual browser store color swatches were separately inspected. This does not prove all-class real-scene accuracy.
- The owned local trial at http://127.0.0.1:8810/ now runs that exact V3 candidate using separately copied local data/recordings. Original V2 executable, configuration and recordings are preserved. Cameras stayed disabled; remote/LAN preview was not started. Existing bot login is retained locally, with no assistant-generated real messages.
- Fresh physical-device and phone acceptance was requested using a cup-only selection, new event playback and the user's own WeChat commands. Result remains pending; historical V2 acceptance is not reused. No V3 GitHub publication or formal AppData upgrade has occurred.

Complete local evidence is retained under ignored `validation-local/`: `v3-label-final-full-tests.log`, `v3-build-record.json`, `v3-exe-*.json`, `v3-exe-market-flow-*-report.json`, `v3-official-download-exe.json`, `v3-root-visual-checks.json`, `v3-store-ui-inspection.json` and `v3-local-trial.json`. Earlier failed/unsuitable fixture receipts remain intact.


## Review Patches and First Physical Trial

Three independent read-only reviewers examined the complete baseline diff. Small implementation patches closed source-epoch confirmation carry-over, stale partial-hit expiration, permanently consumed busy frames, invalid persisted-settings recovery, non-object download JSON, mode-generation races, and camera-toggle updates racing transactional configuration replacement. The store now separates the actual running targets/confidence from draft selections. New guarded HTTPS response reads cover status/header/body deadlines and cancellation, with one retained opening slot for OS connection/DNS work that Python cannot forcibly terminate; this does not claim that OS DNS itself is killed. Independent follow-up review closed the source/API concerns and the transport's Python 3.12 handler-signature and CONNECT timeout-restoration issues.

The patched source passed 300/300 functional tests and 18/18 packaging tests. A first attempted added socketpair test violated the offline-test rule; it was replaced with a controlled in-memory wire fixture, and the failed log remains preserved. The final transport-specific suite passed 25/25. No model, threshold, category palette or renderer was altered for these boundary fixes.

The first physical V3 cup-only trial used the historical ba5e92cb candidate. The user reported playable computer video but missing boxes/labels, and the natural command “今日事件” was rejected. Local inspection found a cup label in the first frame only; later the cup remained visible without stable detections. Five sampled private frames did not produce cup detections at the configured 0.35 threshold in any of the three pinned models; this is a real-scene recognition limitation still requiring a controlled follow-up, not a passed household accuracy test. Private footage/frames remain local under ignored paths and are not publication inputs.

The user's reported “今日事件” is now accepted as the same date selector as “今天事件”, sharing the original camera resolution, authorization, snapshot and Chinese-sequence playback behavior. The 38-test WeChat event suite passed after this alias patch, including strict rejection cases. The subsequent build input set is recorded in ignored `v3-today-alias-source-freeze.json`; source-set SHA256 `3012bb95804cfdce498c834b5daf809a0a73b3ae8831d78cab22f9502a7496ea`. No physical or phone result from the earlier candidate is silently transferred to the reviewed build.


## Reviewed Candidate Delivery (2026-10-19)

The reviewed Windows EXE is 139,168,236 bytes, SHA256 `fcf0f6395d1fcfa49a2c0ef14b6bdfbc8acd58de812311db2b7e06d4e1adcb64`. Its 44 build input digests remain unchanged. Embedded assets/modules, six startup checks, ten isolated preview checks and four simulated-event checks passed. Independent real-graph checks passed eight cases: YOLOX Nano 0.5 person/dog/car/both, NanoDet 0.35 person/both, YOLOX-S 0.5 person/both. The dog flow used the actual compiled “今日事件” command, then “播放第一条”, and verified AES-decoded bytes exactly equal the finished labeled MP4. Root separately viewed decoded public frames from all three models and confirmed readable car/dog labels and correct palette colors.

Fresh empty compiled apps passed official YOLOX Nano download/hash/native apply/restart using both the normal HTTPS path and a temporary loopback CONNECT relay. The relay forwarded encrypted traffic only, with normal certificate verification and no TLS interception; both GitHub and its release-assets redirect were exercised. This validates the new transport on this machine and does not claim every enterprise proxy or OS resolver configuration.

The reviewed EXE has replaced only the owned local V3 test process at http://127.0.0.1:8810/, using the same isolated trial data and retaining the user's cup selection, camera preferences, recordings and bot login. Original V2 files/data and the formal AppData install remain preserved. The reviewed build's fresh physical cup/phone re-test has been requested and is pending. The earlier cup-only failure remains a known real-scene limitation; public dog/car proofs never replace it. No V3 publication has occurred.

Ignored evidence: `v3-review2-build-record.json`, `v3-review2-exe-{assets,startup,preview,events}.json`, `v3-review2-exe-market-flow-*-report.json`, `v3-review2-official-download-{exe,connect}.json`, `v3-root-visual-checks.json`, `v3-reviewed-local-trial.json`, `v3-review/triage.json`. Source/compiled completion and physical acceptance are separate.


## Suggested Review Order

**事务切换**

- 先验证并保存，再结束旧事件和切换模型。
  [hub.py:341](../../nvr/hub.py#L341)

- 集中校验可选模型、目标与置信度。
  [algorithm_catalog.py:89](../../nvr/algorithm_catalog.py#L89)

**下载边界**

- 逐次读取检查期限，保留标准 TLS。
  [model_transport.py:9](../../nvr/model_transport.py#L9)

- 迟到连接保留唯一槽位并关闭响应。
  [model_store.py:176](../../nvr/model_store.py#L176)

**事件与时序**

- 同帧共享推理，限制超时和退役任务。
  [algorithm_runtime.py:67](../../nvr/algorithm_runtime.py#L67)

- 按新鲜结果确认事件，跨 epoch 重置。
  [hub.py:369](../../nvr/hub.py#L369)

**颜色与文字**

- 离线中文字体绘制分类标签和描边。
  [event_labels.py:96](../../nvr/event_labels.py#L96)

- 画框复用识别结果，失败保留原画面。
  [event_overlay.py:115](../../nvr/event_overlay.py#L115)

**界面与指令**

- 管理接口独立保护，异常配置可恢复。
  [server.py:519](../../nvr/server.py#L519)

- 展示实际生效项和待应用选择。
  [app.js:342](../../web/app.js#L342)

- 今日与今天共用严格日期及摄像头选择。
  [clawbot.py:528](../../nvr/clawbot.py#L528)

**验证与打包**

- 覆盖网络、配置失败与并发应用边界。
  [test_algorithm_store.py:57](../../tests/test_algorithm_store.py#L57)

- 覆盖多目标语义及媒体回传。
  [test_algorithms.py:32](../../tests/test_algorithms.py#L32)

- 验证完整中文标签、颜色和原图回退。
  [test_event_labels.py:16](../../tests/test_event_labels.py#L16)

- 核验包内目录、字体、模型和许可。
  [package_windows_release.py:63](../../scripts/package_windows_release.py#L63)
