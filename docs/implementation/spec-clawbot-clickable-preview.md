---
title: '微信预览独立网址回复'
type: 'bugfix'
created: '2026-10-19'
status: 'done'
route: 'one-shot'
---

# 微信预览独立网址回复

## Intent

**Problem:** 用户确认预览画面能从手机外网访问，但微信聊天中的网址不能直接点击，需要复制到浏览器。此前手机画面验收不能当作微信内直接点击的验收。

**Approach:** 只调整成功回复的文字排版：用现有文字消息接口单次发送完整原始网址，不加标题、说明、Markdown 或 HTML，不改动 `#t=` 授权片段、10 分钟有效期、选路、权限、错误回复或消息收据。公开协议没有网页卡片类型；该兼容性试验不保证微信客户端会自动识别链接，须由用户在手机单独确认。163 项离线检查及新版 EXE 的启动、模拟微信预览检查通过，独立盲审未发现新增问题。手机直接点击仍待验收，GitHub 未更新。

## Suggested Review Order

**回复内容与既有权限**

- 成功分支只发原始网址；沿用选路及失败处理。
  [clawbot.py:817](../../nvr/clawbot.py#L817)

**消息契约与进程验证**

- 检查完整网址单次发送，以及实际文字消息结构。
  [test_preview.py:108](../../tests/test_preview.py#L108)
- 检查 EXE 真实进程的单条回复和授权片段，再读取选定画面。
  [check_clawbot_preview.py:330](../../scripts/check_clawbot_preview.py#L330)

**手机验收边界**

- 区分移动网络画面可用与微信内点击打开；说明剩余人工验收。
  [REMOTE_PREVIEW.md:1](../REMOTE_PREVIEW.md#L1)
