# 第三方组件源码与构建信息

## V3.0 算法商城

V3 继续使用现有 OpenCV DNN CPU，不新增 Ultralytics、PyTorch、ONNX Runtime 或 GPU 依赖，不下载或执行远程 Python/PT 代码。商城启用后，NanoDet 的同一 COCO 图用于选中类别的事件判断和录像标注；旧版模式继续保留独立人物画框行为。[v3.1.0 第三方源码资料 ZIP](https://github.com/zhangxutonyace-bit/MiniNVR2-Farm-NVR/releases/download/v3.1.0/MiniNVR2-v3.1.0-third-party-sources.zip)与 Windows 包一同提供，摘要见同版 [SHA256SUMS.txt](https://github.com/zhangxutonyace-bit/MiniNVR2-Farm-NVR/releases/download/v3.1.0/SHA256SUMS.txt)；包内另保留许可证和固定来源记录。

V3.1 实时预览继续复用这些模型、中文字体和 OpenCV DNN，不新增模型库或系统字体依赖；下载的 YOLOX 权重仍由用户另行获取，不放进便携包。

| 模型 | 精确大小 | SHA-256 | 固定官方来源 / 输入配置 |
| --- | --- | --- | --- |
| 内置 NanoDet | 3,800,954 字节 | `4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186` | 下方固定 OpenCV Zoo 来源；RGB 416 归一化、居中补边 |
| YOLOX Nano | 3,659,407 字节 | `c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d` | [官方 0.1.1rc0 模型](https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx)；BGR 416、原始浮点值 0–255、左上补边 114 |
| YOLOX-S | 35,858,002 字节 | `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` | [固定 OpenCV Zoo 模型](https://media.githubusercontent.com/media/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/object_detection_yolox/object_detection_yolox_2022nov.onnx)；RGB 640、原始浮点值 0–255、左上补边 114 |

YOLOX 两图均为未解码的 80 类输出，按官方 grid/stride、指数宽高、objectness × class confidence 解码。本应用先取全部 80 类最高类别，再按用户勾选过滤，并按类别独立 NMS，保留重叠的人/手机等不同目标，总框数有界。该 NMS 与 Nano 原始演示的默认跨类 NMS 不同。

YOLOX 使用 [Apache-2.0 许可证](https://github.com/Megvii-BaseDetection/YOLOX/blob/6ddff4824372906469a7fae2dc3206c7aa4bbaee/LICENSE)。许可及模型/适配来源保留在 `third_party/YOLOX/`，固定目录及类别映射在 `third_party/AlgorithmCatalog/catalog.json`。源码仓库和 EXE 不内置下载后的 YOLOX 权重；仅在用户点击下载时从固定官方来源获取，按大小、摘要和本机兼容性校验后安装到持久数据目录。下方 v2.4.0 条目为历史版本说明。

## V3.0 中文事件标签字体

商城类别标签使用随程序附带的 **MiniNVR Label Sans SC** 小字集，派生自 [Noto Sans CJK SC Regular 2.004](https://github.com/notofonts/noto-cjk/tree/523d033d6cb47f4a80c58a35753646f5c3608a78)，采用 **SIL Open Font License 1.1**。保留原始版权 `© 2014-2021 Adobe`，主字体名与 PostScript 名已改为 MiniNVR Label Sans SC / MiniNVRLabelSansSC-Regular。上游作者不为派生版背书。

- 固定上游提交：`523d033d6cb47f4a80c58a35753646f5c3608a78`（tag `Sans2.004`）；[原字体文件](https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf) SHA-256 为 `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`。
- 派生文件：`fonts/mininvr-labels-sc-regular.otf`，14,440 字节，SHA-256 为 `6586b77fc663de9718099b498cdcbc7aa301d33f3551445243535fc14e61ea00`。
- 只保留13个类别标签所需汉字、可打印 ASCII 及“识别目标”，共123码点/124字形；移除无关字形、提示及未用布局表，未重画字形。FontTools 4.67.0 只用于字体准备，不加入运行时；程序使用已有 Pillow/FreeType，离线文件加载，不依赖系统字体。
- `third_party/LabelFont/OFL.txt`、`NOTICE.txt` 保留许可证和修改声明原文，`SOURCE.json` 保存公开来源、摘要、改名及字符范围，不含本机路径。这些材料与字体一起打入 EXE，下载包另含相应第三方许可。

以下为 MiniNVR2 2.4.0 Windows x64 发布包的历史组件记录。原有 Python、OpenCV、FFmpeg 等依赖版本沿用2.3.0，本版新增下述 NanoDet 离线人物定位模型及其 Apache-2.0 许可；模型不改变原有事件触发算法。原有来源资料核对日期为2026-10-18，依赖文件与新增模型于2026-10-19复核。NetBird 客户端未内置分发，由用户另从其官网下载。
程序 ZIP 内的 `THIRD_PARTY_NOTICES/` 保存随包许可证；本页提供源代码和构建来源。

## v2.4.0 新增：离线人物画框模型

自动/Haar 模式的人物框使用 OpenCV Zoo 的 NanoDet-m-plus-1.5x_416，仅取 COCO `person`（索引 0），由现有 OpenCV DNN 在 CPU 上推理。它独立于事件触发判断，不在运行时下载模型，不使用云端推理。

- 文件：`models/object_detection_nanodet_2022nov.onnx`，3,800,954 字节。
- SHA-256：`4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186`。
- [固定上游版本及说明](https://github.com/opencv/opencv_zoo/tree/25f423d0e04c31a17254620e58febd7386da523b/models/object_detection_nanodet)。
- [精确模型下载](https://media.githubusercontent.com/media/opencv/opencv_zoo/25f423d0e04c31a17254620e58febd7386da523b/models/object_detection_nanodet/object_detection_nanodet_2022nov.onnx)。
- Apache-2.0 [上游许可证](https://github.com/opencv/opencv_zoo/blob/25f423d0e04c31a17254620e58febd7386da523b/models/object_detection_nanodet/LICENSE)，本地保留于 `third_party/NanoDet/LICENSE`；`SOURCE.json` 保存来源、固定提交及摘要。

构建会先核对模型大小和摘要，再将模型与许可证打入独立 EXE。发布打包会直接检查 EXE 内的模型/许可证字节，并将许可证和来源清单复制至 `THIRD_PARTY_NOTICES/NanoDet/`。不额外引入 PyTorch、ONNX Runtime 或 GPU 运行时。

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
