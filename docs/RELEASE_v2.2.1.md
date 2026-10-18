Windows 64 位便携版现已提供下载，完整解压后双击 `MiniNVR2.exe` 即可运行，无需额外安装 Python 或 FFmpeg。

### 下载与运行

1. 下载下方的 **MiniNVR2-v2.2.1-windows-x64.zip**，完整解压到自己有写入权限的文件夹。
2. 双击其中的 **MiniNVR2.exe**，浏览器会打开 `http://127.0.0.1:8800`。
3. 在“摄像头”页添加自己的 RTSP 地址。退出时使用托盘菜单“退出”或网页设置页“退出程序”。

配置和录像默认保存在程序旁的 `data` 文件夹。更换版本时请保留该文件夹。

### 本版内容

- 多路摄像头网页预览、事件录像、定时抽帧和历史回放。
- 可选微信 ClawBot 抽帧录像查询；该功能默认关闭，仍需真实账号收发验证。
- 下载包附中文说明、第三方组件许可与来源；`SHA256SUMS.txt` 可用于核对文件完整性。
- 第三方源码资料另放在 `MiniNVR2-v2.2.1-third-party-sources.zip`，日常运行只需 Windows 便携包。

本程序使用包含 GPLv3+ 组件的 FFmpeg 构建。原始许可、上游构建说明及对应来源资料随包提供，详见 [第三方组件来源](https://github.com/zhangxutonyace-bit/MiniNVR2-Farm-NVR/blob/main/docs/THIRD_PARTY_SOURCES.md)。

### 验证范围

源码的 30 项离线测试已通过；本机 Windows EXE 的启动、内置 FFmpeg 合成视频预览、端口冲突和退出流程已验证。目标为 Windows 10/11 x64，其他电脑兼容性、真实摄像头和现场检测准确率仍需按实际环境确认。本下载包未做代码签名。
