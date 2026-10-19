---
title: '电脑摄像头接入，复用既有检测与录像'
type: 'feature'
created: '2026-10-19'
status: 'done'
baseline_commit: '14784afd0b76d634d9d2532edde0cc340a09a384'
context: []
---

<frozen-after-approval reason="用户确认后的需求边界">

## Intent

**Problem:** 平台尚不能直接接入电脑内置、USB 摄像头作为画面来源。

**Approach:** 仅新增电脑摄像头选择和接入；预览、现有行人检测算法、事件自动录像、回放和微信查询全部复用既有流程。

## Boundaries & Constraints

**Always:** 选定才采集；单次打开设备，共享画面、不采麦克风；保存稳定设备身份。沿用现有检测模式、开关、确认次数、离开延时及冷却；保留既有配置及权限；隔离测试，真人验收另记。

**Ask First:** 安装驱动、改变系统权限，或向外传输真实镜头画面。

**Never:** 新增或更换检测算法、模型、检测模式；自动换镜头、付费服务、人脸身份识别、主动通知、自动发布。不把既有事件录像描述为新增功能，不宣称识别准确率得到提升。

## I/O & Edge-Case Matrix

| 场景 | 输入 | 结果/错误处理 |
| --- | --- | --- |
| 添加 | 已列出的设备 | 保存、启用并预览；重复设备拒绝 |
| 异常 | 无设备、占用、权限拒绝、拔出 | 显示原因，限时重试；不换镜头、不阻塞界面 |
| 事件 | 连续命中/人员离开 | 按既有确认、延时、冷却规则生成 MP4 |
| 停用 | 关闭检测/禁用/删除/退出 | 停录像；禁用、删除、退出释放设备 |
| 换源 | 同 ID 改设备 | 重建采集，旧预览链接失效 |

</frozen-after-approval>

## Code Map

- `nvr/camera.py`：采集与录像；`nvr/hub.py`：检测和事件调度。
- `nvr/config.py`, `nvr/server.py`：配置/API；`nvr/preview.py`：授权。
- `web/index.html`, `web/app.js`：界面。

## Tasks & Acceptance

**Execution:**
- [x] `nvr/local_camera.py`（新增）、`nvr/ffmpeg.py`：限时 DirectShow 枚举、video/alternative-name 配对、单路 JPEG 采集；排除音频/未知类型，不经 shell。
- [x] `nvr/config.py`, `nvr/server.py`：受认证的设备列表及添加；校验类型、设备身份和重复项；旧配置兼容。
- [x] `nvr/local_camera.py`, `nvr/hub.py`, `nvr/preview.py`：统一来源身份，覆盖 worker 重建及所有预览票据（两类预览均在 `preview.py`）。
- [x] `nvr/camera.py`, `nvr/ffmpeg.py`：独立线程编码共享 JPEG 为 H.264 MP4；有界缓存、超时停止、EOF 收尾；不阻塞取流，不暴露未完成录像。
- [x] `nvr/camera.py`, `nvr/hub.py`：将本机画面送入既有检测接口和事件状态机；不改 `nvr/detect.py` 算法、模式或事件触发规则。
- [x] `web/index.html`, `web/app.js`：“电脑摄像头”卡片提供刷新、选择、名称、添加并启用；显示检测/错误；总检测关闭时提示。
- [x] `tests/test_local_camera.py`（新增）、`tests/run.py`：新增24项并注册到local/all；覆盖矩阵、编码器阻塞、设备只开一次、短片时轴及来源切换；旧媒体、生命周期和预览测试保留回归。
- [x] `README.md`, `docs/LOCAL_CAMERA_QUICK_START.md`（新增）、`docs/VALIDATION.md`：操作、检测限制及分级验收记录。

**Acceptance Criteria:**
- Given 本机源在线，when 真人进入再离开，then 事件录像可在回放中播放，预览持续更新。
- Given 已有 RTSP 与微信配置，when 添加本机源，then 原流程和权限不变，授权查询能使用新源。
- Given 已配置检测模式及触发参数，when 新增本机源，then 复用相同算法和规则，不引入 HOG 或其他新模式。

## Spec Change Log

- 2026-10-19：用户明确现有行人检测不是新增功能。删除 HOG、新模式及检测算法改造；新增范围收敛为本机摄像头来源，保留单路采集、事件编码适配和既有授权边界。
- 2026-10-19：独立审查后修复同一构造窗口来源身份不一致、慢供帧时轴压缩；真实设备检查修复枚举缩进兼容。均为实现补丁，冻结需求与原检测算法未改。

## Design Notes

本机录像复用 JPEG，清晰度随共享取流。用 EOF 收尾，不能写入交互退出字符；短片按可解码性判断。编码适配仅解决本机设备重复占用，事件功能和检测算法沿用现有。

## Verification

**Commands:**
- `.venv\Scripts\python.exe -m tests.run local`：24项通过，无跳过；真实FFmpeg处理合成JPEG。
- `.venv\Scripts\python.exe -m tests.run all`：最终188项全部通过，无跳过，19.014秒。
- `.venv\Scripts\python.exe -m unittest tests.test_windows_release`：9项通过。
- `.venv\Scripts\python.exe build\build_exe.py`：v2.4.0候选构建成功；同一EXE的6项启动、10项模拟预览、4项模拟事件检查通过。保留原安装程序，未发布GitHub。

**Manual checks:** 最终EXE真实HD Camera预览更新、停用释放通过；用户按进入镜头走动、离开步骤确认“生成事件录像，而且能播放”。EXE SHA-256为 `095e2c98b79cadd0657640b67abb349758b20eccab225e06bdfaeb942ad668c6`。真实画面未外发，微信路径仅模拟；其他设备、实际拔插与长期稳定性未验收。

**Planning evidence:** 2026-10-19 枚举发现 HD Camera，未采集。设备接入依据：[FFmpeg DirectShow](https://ffmpeg.org/ffmpeg-devices.html#dshow)。

**Implementation evidence:** `Cam.spec`为worker创建时的副本，避免异步换源窗口把新设备画面配给旧来源身份。新来源票据失效测试覆盖局域网及远程预览。编码失败且无输出时仍有界面错误与日志；本机事件先写`.part.mp4`，验证可解码后再改为正式文件名，未完成文件不可回放。

## Suggested Review Order

**设备接入与来源边界**

- 从已列设备添加，沿用管理认证
  [server.py:202](../../nvr/server.py#L202)

- 采集和授权使用同一来源副本
  [camera.py:102](../../nvr/camera.py#L102)

- 两种预览均核对实际 worker 来源
  [preview.py:339](../../nvr/preview.py#L339)

**共享画面与录像**

- 事件编码消费共享 JPEG，不再打开设备
  [camera.py:381](../../nvr/camera.py#L381)

- 慢供帧保留墙钟间隔
  [ffmpeg.py:323](../../nvr/ffmpeg.py#L323)

- 独立写入与限时停止
  [local_camera.py:65](../../nvr/local_camera.py#L65)

**检查与使用**

- 并发构造和来源变更回归
  [test_local_camera.py:179](../../tests/test_local_camera.py#L179)

- 操作与实机验收边界
  [LOCAL_CAMERA_QUICK_START.md:1](../LOCAL_CAMERA_QUICK_START.md#L1)
