---
title: 'ClawBot 事件录像查询与 Windows 发布'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_commit: '805696fa4b0b4147fcf471565366a108c64b1f79'
context: []
authorization: '用户已明确授权实现、测试、重新打包并更新公开 Release。'
---

<frozen-after-approval reason="用户原始目标与现有授权范围">

## Intent

**Problem:** 微信目前只回传按日抽帧，无法查看已经保存的事件片段。

**Approach:** 在现有授权和视频上传流程中增加事件列表、筛选、分页与单段播放；测试后发布 v2.2.2 Windows 便携版。

## Boundaries & Constraints

**Always:** 保留所有者/白名单与 context_token 校验；仅发送配置中启用摄像头的已结束录像；默认不启用微信；使用临时合成素材和模拟收发测试；保留旧 Release。

**Ask First:** 若需要真实微信账号扫码、实际外发私人录像，交由用户操作；本次模拟验收不需要这些操作。

**Never:** 不导入真实配置、不读取原始录像、不更改 iLink 协议、不添加主动推送，不把模拟测试写成微信真实送达。

## I/O & Edge-Case Matrix

| 场景 | 输入/状态 | 结果 | 错误处理 |
| --- | --- | --- | --- |
| 列表 | 今天事件、昨天事件 农场、事件 2026-10-18 1号 | 默认今天，按摄像头/日期筛选，最新在前，每页10条 | 无结果说明仅列已完成录像 |
| 单段 | 最新事件、今天事件 第2条、播放第2条 | 最新或指定序号只发送一段 | 无列表、过期或越界提示重查 |
| 分页 | 下一页、上一页 | 继续本人的固定列表，全局序号 | 边界提示，不切换他人列表 |
| 安全 | 录像中、重封装、临时文件、越界路径 | 全部排除，发送前重新核验 | 不发送也不泄漏磁盘路径 |
| 输入错误 | 非法日期、未知摄像头、无效序号 | 明确提示 | 不退回全摄像头或抽帧 |
| 视频限制 | 文件超过配置上限、服务拒绝 | 不把失败报告为成功 | 大小检查先于读取/上传 |

</frozen-after-approval>

## Code Map

- `nvr/clawbot.py`：命令、本人列表快照、视频发送。
- `nvr/recordings.py`：安全枚举和重新核验已完成事件。
- `nvr/hub.py`、`nvr/camera.py`：录像/重封装交接状态。
- `tests/test_clawbot_events.py`：消息入口、文件与真实合成视频闭环。
- `web/index.html`：微信指令帮助。
- `scripts/package_windows_release.py`：已验收 EXE 与组件资料打包。

## Tasks & Acceptance

**Execution:**
- [x] 新增已完成事件目录与安全查询，修补停止录像交接窗口。
- [x] 增加事件消息路由、本人15分钟列表缓存、分页与单段取回。
- [x] 加强发送大小预检与服务错误判断，保持原抽帧可用。
- [x] 增补离线测试、模拟CDN/AES完整闭环。
- [x] 更新帮助、使用说明、版本和验收记录，准备 v2.2.2 打包发布材料；测试审查完成后执行发布。

**Acceptance Criteria:**
- Given 已授权用户和已完成片段，when 事件查询及选片，then 正确筛选并只上传被选视频。
- Given 新录像进入目录，when 按已有列表序号播放，then 仍选原列表对应片段。
- Given 非法输入/未授权用户/写入中文件，when 请求，then 不上传视频。
- Given 本地合成MP4，when 模拟完整收发，then 密文可还原原视频且消息字段正确。
- Given 测试和冻结程序验收通过，when 发布，then 新 Release 可匿名下载且摘要与本机一致。

## Spec Change Log

## Design Notes

事件请求在设备列表命令之前识别，避免“摄像头”一词误路由；日期数字、页码和片段序号不得当作摄像头序号。列表按录像文件名时间排序，缓存按用户隔离、过期清理并限制数量。默认只列文字，用户选片或请求最新时才上传。

## Verification

- `.venv/Scripts/python.exe -m tests.run all`：59/59 通过（无跳过），无真实网络外发。
- `.venv/Scripts/python.exe build/build_exe.py`：生成 v2.2.2。
- `.venv/Scripts/python.exe scripts/check_startup.py dist/MiniNVR2.exe`：冻结程序启动、视频和退出通过。
- `.venv/Scripts/python.exe scripts/check_clawbot_events.py [dist/MiniNVR2.exe]`：源码与冻结程序均通过本机假 iLink/CDN、AES 原片还原、正常退出。
- 打包时检查 ZIP 完整性、EXE 与记录摘要；发布后核对 GitHub 资产摘要及匿名下载。

## Review Outcome

三路独立审查后完成六类修补：旧摄像头序号、日期数字边界、翻页失败重试、扫描忙碌状态、录像启动登记与打开后的文件身份。后续只读复核未发现新的具体问题。保留协议允许省略零返回码的行为，显式空值或错误码拒绝；接口接受不等于手机送达。

## Suggested Review Order

**事件入口与选择器**

- 先区分事件、分页与旧抽帧命令，避免误路由。
  [clawbot.py:619](../../nvr/clawbot.py#L619)

**文件与录像生命周期**

- 只枚举安全目录中的已完成事件，限制缓存大小。
  [recordings.py:62](../../nvr/recordings.py#L62)

- 大小预检与打开后的身份复核先于读取上传。
  [clawbot.py:319](../../nvr/clawbot.py#L319)

- FFmpeg 写入前登记路径，失败清理状态。
  [camera.py:364](../../nvr/camera.py#L364)

- 从录像到重封装的交接持续保持忙碌。
  [hub.py:171](../../nvr/hub.py#L171)

**验收与分发**

- 覆盖权限、选择器、分页和录像状态边界。
  [test_clawbot_events.py:69](../../tests/test_clawbot_events.py#L69)

- 真实启动程序，用本机假服务校验原片还原。
  [check_clawbot_events.py:170](../../scripts/check_clawbot_events.py#L170)

- 要求启动与事件验收证据匹配版本后打包。
  [package_windows_release.py:156](../../scripts/package_windows_release.py#L156)
