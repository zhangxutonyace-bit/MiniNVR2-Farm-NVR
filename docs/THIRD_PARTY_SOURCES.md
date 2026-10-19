# 第三方组件源码与构建信息

本文件对应 MiniNVR2 2.2.3 Windows x64 发布包，沿用2.2.2的相同第三方组件；来源资料核对日期为 2026-10-18，本轮依赖版本及包内文件于2026-10-19复核。
程序 ZIP 内的 `THIRD_PARTY_NOTICES/` 保存随包许可证；本页提供源代码和构建来源。

## FFmpeg 命令行程序

独立的 FFmpeg 由 imageio-ffmpeg 0.6.0 Windows wheel 提供，版本为
`7.1-essentials_build-www.gyan.dev`。本机核对其文件与
[Gyan 官方 7.1 essentials 发布包](https://github.com/GyanD/codexffmpeg/releases/tag/7.1)
中的 `ffmpeg.exe` SHA256 一致。

imageio-ffmpeg Python 包装层使用 BSD-2-Clause；**其中的独立 FFmpeg 程序使用 GPLv3 或后续版本**。
它以 `--enable-gpl --enable-version3 --enable-static` 构建，含 x264、x265 等第三方库。
ZIP 内 `THIRD_PARTY_NOTICES/FFmpeg/` 包含 Gyan 原始 `README.txt`、`LICENSE` 和
`BUILD_CONFIGURATION.txt`（程序实际输出的版本、完整 configure 参数及许可证）。

- FFmpeg 核心源码精确提交：[b08d7969c5](https://github.com/FFmpeg/FFmpeg/commit/b08d7969c5)。
- [精确提交核心源码下载](https://github.com/FFmpeg/FFmpeg/archive/b08d7969c5.tar.gz)。
- 同版 Release 的第三方源码资料附件含 `ffmpeg-b08d7969c5-core-source.tar.gz`。
- 核心源码内的 `configure`、`Makefile`、`INSTALL.md` 和 `doc/` 提供 FFmpeg 构建入口；
  实际构建工具为 GCC 14.2.0（MSYS2）。原 Gyan README 还保存启用组件与外部依赖版本。

该核心归档**不等同于整个 Gyan 静态二进制的完整对应源码**。静态依赖还需下表所列源代码，
并需相应依赖构建脚本、配置和可能的补丁。以下版本按 Gyan 原始 README 逐项记录；
标为“已核对提交”的链接已在官方仓库确认存在，版本归档入口未全部下载或重建。

| 组件 | Gyan README 记录版本 | 上游源码下载或项目入口 | 核对情况 |
| --- | --- | --- | --- |
| AMF | `v1.4.34-6-g3db6164` | [源码](https://github.com/GPUOpen-LibrariesAndSDKs/AMF/archive/3db6164375ca62337e068193658f7cb10f0c42f9.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| aom | `v3.10.0-110-g3817481261` | [源码](https://aomedia.googlesource.com/aom/+archive/3817481261f8675ef24f327b3dbcdcebbfa389e1.tar.gz) | 官方 Gitiles 已核对提交 |
| AviSynthPlus | `v3.7.3-75-gc6649361` | [源码](https://github.com/AviSynth/AviSynthPlus/archive/c6649361a30fe8db128cb4d0cb71c578a468d824.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| ffnvcodec | `n12.2.72.0-1-g9934f17` | [源码](https://github.com/FFmpeg/nv-codec-headers/archive/9934f17316b66ce6de12f3b82203a298bc9351d8.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| freetype | `VER-2-13-3` | [源码](https://github.com/freetype/freetype/archive/42608f77f20749dd6ddc9e0536788eaad70ea4b5.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| fribidi | `v1.0.16` | [源码](https://github.com/fribidi/fribidi/archive/68162babff4f39c4e2dc164a5e825af93bda9983.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| gsm | `1.0.22` | [源码](https://www.quut.com/gsm/gsm-1.0.22.tar.gz) | 官方版本归档入口 |
| harfbuzz | `10.0.1-1-gc7ef6a2e` | [源码](https://github.com/harfbuzz/harfbuzz/archive/c7ef6a2ed58ae8ec108ee0962bef46f42c73a60c.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| lame | `3.100` | [源码](https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz) | 官方项目版本归档入口 |
| libass | `0.17.3-32-g5298859` | [源码](https://github.com/libass/libass/archive/5298859c298d3c570d8d7e3b883a0d63490659b8.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| libgme | `0.6.3` | [源码](https://github.com/libgme/game-music-emu/archive/e76bdc0cb916e79aa540290e6edd0c445879d3ba.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| libopencore-amrnb | `0.1.6` | [源码](https://downloads.sourceforge.net/project/opencore-amr/opencore-amr/0.1.6/opencore-amr-0.1.6.tar.gz) | 与 amrwb 共用源包 |
| libopencore-amrwb | `0.1.6` | [源码](https://downloads.sourceforge.net/project/opencore-amr/opencore-amr/0.1.6/opencore-amr-0.1.6.tar.gz) | 与 amrnb 共用源包 |
| libssh | `0.11.1` | [源码](https://www.libssh.org/files/0.11/libssh-0.11.1.tar.xz) | 官方版本归档入口 |
| libtheora | `1.1.1` | [源码](https://github.com/xiph/theora/archive/7ffd8b2ecfc2d93ae5e16028e7528e609266bfbf.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| libwebp | `v1.4.0-97-g220ee529` | [源码](https://github.com/webmproject/libwebp/archive/220ee52967c121adf3c690fbd0605a7de6371943.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| oneVPL | `2.13` | [源码](https://github.com/intel/libvpl/archive/5f6bd8a1e753c8f63a3fd8b36894d6968b808a6d.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| openmpt | `libopenmpt-0.6.19-9-g1a29ac622` | [源码](https://github.com/OpenMPT/openmpt/archive/1a29ac622f4295b6a20ee3c00c045724c18c538f.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| opus | `v1.5.2-21-gff6dea5e` | [源码](https://github.com/xiph/opus/archive/ff6dea5e1a72b8a2aeb7fc3656857d86a420ab89.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| rubberband | `v1.8.1` | [源码](https://github.com/breakfastquay/rubberband/archive/afdb914d0b853f445ef4582720966701527ade5b.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| SDL | `prerelease-2.29.2-377-g1edaad172` | [源码](https://github.com/libsdl-org/SDL/archive/1edaad17218d67b567c149badce9ef0fc67f65fa.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| speex | `Speex-1.2.1-28-g1de1260` | [源码](https://github.com/xiph/speex/archive/1de1260d24e01224df5fbb8b92893106c89bb8de.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| srt | `v1.5.4-rc.0-11-ga7b3711` | [源码](https://github.com/Haivision/srt/archive/a7b3711a8a862e697f641b296743cc7c921f5fba.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| VAAPI | `2.23.0.` | [源码](https://github.com/intel/libva) | 上游项目；Gyan 的 Windows 构建精确修订未确定 |
| vidstab | `v1.1.1-13-g8dff7ad` | [源码](https://github.com/georgmartius/vid.stab/archive/8dff7ad3c10ac663745f2263037f6e42b993519c.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| vmaf | `v3.0.0-95-gd95b69e0` | [源码](https://github.com/Netflix/vmaf/archive/d95b69e03d9401ad9f3532eaab14066427761c35.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| vo-amrwbenc | `0.1.3` | [源码](https://downloads.sourceforge.net/project/opencore-amr/vo-amrwbenc/vo-amrwbenc-0.1.3.tar.gz) | 官方项目版本归档入口 |
| vorbis | `v1.3.7-10-g84c02369` | [源码](https://github.com/xiph/vorbis/archive/84c023699cdf023a32fa4ded32019f194afcdad0.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| vpx | `v1.14.1-371-g32de9c2be` | [源码](https://github.com/webmproject/libvpx/archive/32de9c2becdd5d96d84d37be9f7fb9de43f24a4d.tar.gz) | 官方仓库 API 已核对提交 |
| x264 | `v0.164.3192` | [源码](https://code.videolan.org/videolan/x264) | 上游源仓库；README 未提供提交，未猜测提交映射 |
| x265 | `4.0-6-ga009ec077` | [源码](https://bitbucket.org/multicoreware/x265_git/get/a009ec077.tar.gz) | 官方仓库已核对该提交的源码文件 |
| xvid | `v1.3.7` | [源码](https://downloads.xvid.com/downloads/xvidcore-1.3.7.tar.gz) | 官方版本归档入口 |
| zeromq | `4.3.5` | [源码](https://github.com/zeromq/libzmq/archive/622fc6dde99ee172ebaa9c8628d85a7a1995a21d.tar.gz) | 官方仓库 API 已核对 tag/提交 |
| zimg | `release-3.0.5-150-g7143181` | [源码](https://github.com/sekrit-twc/zimg/archive/71431815950664f1e11b9ee4e5d4ba23d6d997f1.tar.gz) | 官方仓库 API 已核对 tag/提交 |

Gyan README 还启用了 bzlib、GMP、GnuTLS、iconv、libxml2、fontconfig、openjpeg、lzma、zlib，
但其版本表未给这些库的精确版本；当前资料不把它们映射成未经核实的源码修订。
VAAPI 和 x264 的精确上游修订同样没有在该 README 中完整给出。
Gyan 发布仓库未附完整依赖构建脚本；本项目保存已取得的上游材料，未宣称完成该静态工具链的重建验证。

FFmpeg CLI 通过独立进程调用，与 OpenCV 随附的 FFmpeg DLL 是两个组件。
OpenCV-Python 4.12.0.88 的许可证与完整第三方声明位于
`THIRD_PARTY_NOTICES/opencv-python-4.12.0.88/`；其 FFmpeg DLL 使用 LGPLv2.1 或后续版本，
来源及构建入口见 [OpenCV-Python 4.12.0.88](https://github.com/opencv/opencv-python/tree/4.12.0.88)
及其中固定的 FFmpeg 构建资料。修改该 DLL 后可从应用源码重新打包使用。

## pystray 0.19.5

pystray 使用 LGPLv3。包内同时提供 `COPYING`（GPLv3）和 `COPYING.LGPL`。
[上游 v0.19.5 完整源码](https://github.com/moses-palmer/pystray/archive/refs/tags/v0.19.5.tar.gz)
另存为 Release 第三方源码资料附件中的 `pystray-0.19.5-upstream-source.tar.gz`。
该归档的 13 个 Python 源文件与本次打包环境中实际安装的 pystray 源文件逐字节一致。
PyPI 0.19.5 仅发布 wheel，未发布 sdist，因此使用作者官方 Git tag 归档并提供本地 SHA256。

如需替换或修改 pystray，在 MiniNVR2 源码环境安装修改后的同名 Python 包，然后按
`build/build_exe.py` 重新构建。构建入口及所需固定版本见项目 `requirements.txt`、
`requirements-build.txt`；本项目不限制为调试这些修改所需的逆向分析。

## PyInstaller 6.22.3 与其他依赖

PyInstaller 引导程序使用 GPL 与允许分发所生成应用的特别例外；完整文本随 Windows 包提供。
Release 第三方源码资料附件含官方 `pyinstaller-6.22.3.tar.gz`，其 SHA256 已与
[PyPI 6.22.3 源包元数据](https://pypi.org/pypi/pyinstaller/6.22.3/json) 核对。

其他 Python 运行时依赖的完整版权和许可文本按包名收录于 `THIRD_PARTY_NOTICES/`，
Python 3.12.10、NumPy、Pillow 和 OpenCV 所附第三方声明保留全文。
cryptography 50.0.2 在本构建中报告使用 OpenSSL 4.0.3；其许可不同于 CPython 自带的 OpenSSL 副本，
上游许可入口为 [OpenSSL LICENSE.txt](https://github.com/openssl/openssl/blob/openssl-4.0.3/LICENSE.txt)。

## 下载校验与来源清单

第三方源码资料附件内含 `sources-manifest.json`、`SHA256SUMS.txt`、
`ffmpeg-library-source-refs.json` 与 `pystray-source-verification.json`。
PyInstaller 的 SHA256 同时经过 PyPI 上游摘要校验；GitHub 生成的 FFmpeg、pystray 归档
提供此次下载文件的本地完整性摘要，不将该摘要描述为作者发布的独立签名。
