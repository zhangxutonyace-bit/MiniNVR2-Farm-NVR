---
title: '微信跨网段摄像头预览'
type: 'feature'
created: '2026-10-19'
status: 'done'
baseline_commit: '0c08624bedc7bf4d15b690a6188e4bd18586ee07'
context: []
authorization: '用户要求开发下一版本；优先免费、配置简单；明确手机只使用微信，不安装额外应用。'
---

<frozen-after-approval reason="用户已明确授权开发；手机只用微信是本次最新约束">

## Intent

**Problem:** Existing preview links contain a LAN address and fail when the phone uses cellular data or another Wi-Fi. The PC and cameras must remain on their existing LAN.

**Approach:** Add an opt-in, session-only HTTPS relay for the isolated camera viewer. Use the official NetBird client on the PC; the phone opens the link in WeChat without another app. NetBird currently offers a free individual/small-team tier, with a beta reverse proxy. Treat eligibility, availability and limits as provider-controlled, not a permanent free-service promise.

## Boundaries & Constraints

**Always:** Preserve owner/allowlist and WeChat context authorization, one-camera ten-minute capabilities, fresh-frame checks, camera-source matching, revocation and existing LAN defaults. Relay only a separate loopback viewer. Explain that NetBird terminates HTTPS and can process relayed images. Require an explicit start after every application launch. Use synthetic pictures for automatic checks; distinguish these from actual cellular-phone acceptance.

**Ask First:** User completes provider registration/login and account permissions. Before first real-picture relay, show the provider and scope and obtain activation confirmation. Paid resources, firewall/router changes and persistent service installation need separate authorization. Publishing a new GitHub release is outside this development request.

**Never:** Expose the management application, recordings, RTSP credentials, arbitrary local ports or the whole LAN. Never collect provider passwords or API keys, silently install a VPN/service, change existing network policies, use Cloudflare Quick Tunnel as a production video workaround, or claim external-phone success from local tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected behavior | Failure handling |
| --- | --- | --- | --- |
| Start | Installed official client, connected account and Peer Expose permission | Start dedicated loopback viewer and one owned expose process; report HTTPS readiness | Finite startup deadline; close origin and show actionable Chinese status |
| Missing setup | Client absent, disconnected, outdated, permission denied | No usable remote link; show official setup instructions | Never perform account/policy changes automatically |
| WeChat request | Authorized “预览1号” while remote mode selected | Send that camera's current HTTPS link on its own line | Failed remote session returns an error, never a silent LAN fallback |
| Camera access | Valid token / invalid, expired or another camera | Only fresh frames from the selected source | Deny and clear the phone picture when access ends |
| Stop/failure | Stop button, child exit, settings disable or application quit | Revoke tokens and close origin before terminating owned process | No surviving image origin even if provider cleanup is delayed |
| Restart | Application starts again | Remote session is stopped and old tokens unusable | User deliberately starts a new session |

</frozen-after-approval>

## Code Map

- `nvr/preview.py`: reuse camera-only routes and capability checks; add separate loopback instance and HTTPS link generation.
- `nvr/remote_preview.py`: official-client discovery, bounded expose process, URL parsing, status and cleanup.
- `nvr/remote_bridge.py`: bounded four-route bridge bound only to the current NetBird adapter; fixed upstream loopback viewer.
- `nvr/hub.py`: serialize preview operations against shutdown; select local/remote mode.
- `nvr/config.py`, `nvr/server.py`: validated remote preferences and authenticated local controls.
- `nvr/clawbot.py`: preserve camera selection and receipts, explain the selected connection mode.
- `web/index.html`, `web/app.js`: setup, start/stop and status in existing settings.
- `web/preview.html`, `web/preview.js`: network-neutral mobile instructions and failure recovery.
- `tests/test_remote_preview.py`, `tests/run.py`: isolated fixture-based regression.
- `docs/REMOTE_PREVIEW.md`, `README.md`: setup and precise evidence boundaries.

## Tasks & Acceptance

**Execution:**
- [x] Add a remote viewer bound only to `127.0.0.1`, independent ticket stores, and strict HTTPS-origin validation.
- [x] Add a controller for a trusted installed NetBird binary, fixed argument list without a shell, bounded output/startup and one owned session; never run account or install commands.
- [x] Add local settings/API controls and Hub integration, default off, no persisted active session and no automatic remote restart.
- [x] Update WeChat and mobile UI so selected remote mode has honest readiness/errors and no LAN-only wording.
- [x] Cover matrix and concurrency cases with offline tests; run the full existing suite and a synthetic process smoke check.
- [x] Perform independent review, document setup and preserve current installed app/data until replacement passes checks.

**Acceptance Criteria:**
- Given the phone has only WeChat and an external network, when an authorized user opens a new ready HTTPS link, then the page can retrieve the selected camera without a LAN route; actual phone confirmation is recorded separately.
- Given either type of preview token, when used on the other listener or a management/recording route, then it grants no access.
- Given a failed/closed relay, when a link is requested or an existing page refreshes, then no new picture is available and the user sees a useful recovery instruction.
- Given an existing LAN configuration, when upgrading without enabling remote mode, then LAN behavior and management protection remain unchanged.

## Spec Change Log

- 2026-10-19: Official v0.81.2 source inspection showed normal Windows `expose` targets the peer adapter, not loopback. Added an adapter-specific bridge to the implementation design, retaining the separate loopback capability service. KEEP camera-only routes, independent tickets, explicit session start, no global local-forwarding toggle and no management exposure. Stop the bridge before the loopback viewer and owned client; validate the address from the signed client's connected status and prove it is locally bindable.

## Design Notes

The NetBird process exposes a dynamically allocated bridge port bound only to its own assigned adapter IPv4. The bridge accepts overlay sources, permits only the four read-only viewer paths, preserves the capability header and forwards exclusively to the fixed loopback origin. It has bounded workers, timeout and response size. Its documented cloud session may survive an abrupt client exit for roughly 90 seconds, so local listeners close and tickets revoke before the owned client stops. A provider URL is accepted only as a clean HTTPS origin from the owned client's output; it is never an arbitrary server-side fetch target. Do not log raw subprocess output, account identifiers, tickets or complete viewer links. Production reliability remains subject to real phone testing and provider availability.

## Verification

- Run `.venv/Scripts/python.exe tests/run.py` with UTF-8 and workspace TEMP/TMP; expect existing and new checks to pass without external connections.
- Use fake client/process fixtures and synthetic JPEGs to verify listener binding, readiness, errors, route isolation, token revocation and shutdown.
- Inspect the settings page and phone viewport for readable start/stop/status and network-neutral recovery messages.
- Leave account activation and real mobile-data acceptance visibly pending until the user completes them.


## Development Evidence (2026-10-19)

- Full host regression: 163/163 passed, 12.658 seconds, no skips. Node syntax checks passed.
- Real loopback HTTP plus camera-only bridge and owned fake relay: eight synthetic checks passed, including changing decodable frames, revocation, scope, stale-frame denial and listener closure. The fixture substitutes loopback for the unavailable NetBird adapter; this is not a real provider test.
- Windows EXE startup (six checks), simulated event delivery (four checks), existing LAN preview (ten checks) passed. Embedded archive contains the viewer, connector and bridge modules.
- Independent blind, edge and acceptance reviews completed. Fixed dead-origin readiness and status-reader startup cleanup; deterministic regressions pass. Rejected the blind non-object-JSON finding because the central guard returns 400, confirmed by a new integration test.
- Official NetBird v0.81.2 signed archive and installer hashes matched GitHub metadata. Publisher NetBird GmbH is valid; the actual signature inspector accepts the downloaded executable after the PS5 module-path fix. Help flags and tagged source interfaces were inspected; no account/session was activated.
- Local installed MiniNVR2 is now v2.3.0. Old EXE/private config are backed up; camera specifications and WeChat identity were preserved, and existing WeChat reception resumed. Remote mode remains disabled/stopped and no firewall rule was changed.
- Current installed check: configured LAN-preview address is no longer assigned to this PC; camera is offline. The backup contains that same stale preview address. Do not treat this as successful live-camera acceptance or silently change camera/network settings.
- Real NetBird account connectivity, actual proxy Authorization forwarding, mobile-data/WeChat viewing and sustained operation remain NOT_TESTED. Provider installation/login/activation is the next human-controlled step. Public GitHub release remains v2.2.3.
- Candidate EXE SHA256: e6e32c1a1115dfda774ba4dffa8dcd1eb39d1538d14da255a539552d9285b983; bytes: 135515483.

## Suggested Review Order

**Entry and explicit activation**

- Choose remote mode without silently falling back to an unusable LAN link.
  [hub.py:87](../../nvr/hub.py#L87)

- Require local management authorization and explicit relay confirmation.
  [server.py:452](../../nvr/server.py#L452)

**Network and lifecycle boundaries**

- Own one signed-client session and serialize all listener cleanup.
  [remote_preview.py:351](../../nvr/remote_preview.py#L351)

- Permit four camera routes to one fixed loopback upstream.
  [remote_bridge.py:91](../../nvr/remote_bridge.py#L91)

**Capabilities and phone UI**

- Keep short-lived remote tickets separate from LAN tickets.
  [preview.py:441](../../nvr/preview.py#L441)

- Explain setup, readiness and recoverable failures in Chinese.
  [app.js:349](../../web/app.js#L349)

**Verification and operation**

- Reject unsafe status responses and clean failed status-reader startup.
  [test_remote_preview.py:409](../../tests/test_remote_preview.py#L409)

- Follow the setup steps and retain the unverified external-network boundary.
  [REMOTE_PREVIEW.md:1](../REMOTE_PREVIEW.md#L1)
