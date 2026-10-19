---
title: '发布 v2.4.0 Windows 实测修复版'
type: 'chore'
created: '2026-10-19'
status: 'done'
route: 'one-shot'
---

# 发布 v2.4.0 Windows 实测修复版

## Intent

**Problem:** 已实测通过的电脑摄像头、微信中文序号播放和人物框跟随修复仍只在本机，公开下载与指南停留于 v2.3.0。

**Approach:** 按用户明确授权发布 v2.4.0，沿用同一已验收EXE，补五份中文指南及Release说明。新增同EXE事件验收分发校验，保持v2.3.0远程验收要求；本版手机外网未重测则明确NOT_RETESTED。只上传明确列出的源码和三个发布附件，核验包内文件及匿名完整下载，保留旧Release。独立审查修补一项过强的设备范围描述，16项分发测试通过；未改产品代码或重新构建。

## Suggested Review Order

**发布与验收边界**

- 本版用新事件收据，旧版保留原手机预览校验。
  [package_windows_release.py:336](../../scripts/package_windows_release.py#L336)

- 只公开同EXE的限定验收结果，不复制私有元数据。
  [package_windows_release.py:163](../../scripts/package_windows_release.py#L163)

**下载包与说明**

- 五份中文指南采用记事本可读格式并随包分发。
  [package_windows_release.py:201](../../scripts/package_windows_release.py#L201)

- 提供下载、操作流程、改动和准确的验证范围。
  [RELEASE_v2.4.0.md:1](../RELEASE_v2.4.0.md#L1)

**回归证据**

- 拒绝错误身份、非布尔确认及越界范围。
  [test_windows_release.py:60](../../tests/test_windows_release.py#L60)

- 验证新版五份指南、旧版要求和原EXE字节不变。
  [test_windows_release.py:198](../../tests/test_windows_release.py#L198)
