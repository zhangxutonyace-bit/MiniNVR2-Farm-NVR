# Live preview candidate review — 2026-10-20

This is a local 3.1.0 development record, not a published-release or real-camera acceptance claim.

Three independent reviewers read the complete tracked/untracked patch: a blind adversarial reviewer received only the product diff; a path reviewer could inspect referenced source; an acceptance reviewer also read the frozen spec and its context guide. Review input receipts are in ignored `validation-local/live-preview-review/input.json`.

## Triage

These are implementation patches against requirements already captured in the spec; none requires a new user decision or changes the frozen intent.

| Finding | Disposition | Required correction |
| --- | --- | --- |
| Raw fallback advances the sequence floor faster than a valid delayed renderer can catch up | Patch | Bound whole-frame display buffering, preserve monotonic output and recovery; test continuously advancing capture/read/render |
| Desktop live reads can cross a disable, source/worker/epoch or model change | Patch | Revalidate identity after reading; discard incompatible output |
| Explicit annotated snapshot admits an obsolete worker | Patch | Apply the same before/after checks to `boxes=1` |
| Stop between raw sampling and an ineligible-context return leaks a retired sample | Patch | Enforce stop admission on the early-return branch |
| A retired native call outlives Cam replacement and permits another worker for the same logical camera | Patch | Retain a bounded retirement slot across Cam generations |
| Repeated processing exceptions retry an unchanged job every 20 ms | Patch | Bound failure retry cadence and recover on valid new work |
| Successful raw rendering can hide unavailable/expired semantics | Patch | Distinguish a valid negative decision from unavailable semantic input |
| Ten-frame history drops valid delayed anchors at higher capture rates | Patch | Cover supported rates with a finite frame cap and the same time bound |
| Same-source JPEG size changes can return an incompatible cached image | Patch | Include size in cache admission/publication and invalidate before native work completes |
| Default-interval semantic renewal can outlast the paired display budget without an event writer | Patch | Refresh shared semantics during active preview demand; leave event decisions and both freshness bounds intact |
| Saved per-camera detection disable can precede asynchronous Cam synchronization | Patch | Bind eligibility to the current persisted camera and reject in-flight publication after disable |
| Capability previews omit the model/detection generation guard after cache reads | Patch | Compare context before/after LAN and remote cache reads; discard an incompatible packet |

The request to reject unselected detections before computing a negative is already guaranteed by `AlgorithmRunner`'s selected-class filtering. The market start-to-start cadence is an intentional, documented correction, with existing event thresholds and legacy timing retained. Generic redacted user warnings are intentional; diagnostic-detail suggestions alone do not establish a functional failure. A failed native thread-start slot is safe if a defined camera restart can recover without allowing overlapping workers; that recovery path is checked with the retirement fix.

The old raw snapshot's configuration/source consistency is pre-existing and is recorded separately for future attention. The newly introduced annotated path is in scope here.

## Verification state

All twelve confirmed implementation findings in the table are patched and covered by regressions. Follow-up blind, edge-path and frozen-intent acceptance reviewers report no remaining confirmed blocker. Runtime review input SHA-256: `acfdf1482d447b6e00b88ad4516105a6c0867cf8feb4bc501f3de43e55a052b8`; later documentation only records the final evidence and navigation links.

Final offline suite: 410/410 PASS. Exact r4 Windows executable `89dd6e1b39cf448e5ba88a1d14088ba29577fe9b127662620c0c089f6f33f938` passed one/three-viewer strict public-fixture checks and default-interval preview with no event writer. Source and embedded-resource identity match the build receipt. Details and original failed runs remain in ignored `validation-local`; no old evidence was relabeled for this artifact.

An earlier r2 run exhausted a fixed toggle-to-drain deadline at the source-completion boundary. A read-only independent audit identified startup/drain phase overlap; the observer now records a bounded first-online phase separately and retains all original steady-state gates and every frame. This is a measurement correction, not a product-timing or model change.

Real-camera and phone smoothness remain pending user acceptance. This local candidate has not been pushed or published.
