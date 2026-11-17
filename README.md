# 🎙️ 语音转文字

> 基于 [whisper.cpp](https://github.com/ggml-org/whisper.cpp) 的**本地离线**音视频转文字工具 —— 文件不上传、不联网、带图形界面。

`Windows 10 / 11` · `v1.0.0` · `MIT License`

---

## ✨ 特性

- 🔒 **纯本地离线** —— 音视频不出电脑，无账号、无遥测
- 🖥️ **图形界面** —— 浏览器打开的本地页面，点几下就转完
- 📂 **系统原生选择框** —— 选什么转什么，不会误带同目录文件
- ⚡ **常驻推理引擎** —— 模型只加载一次，批量转写约提速 12 倍
- 🌐 **语言自动检测** —— 中 / 英 / 日 / 韩 / 粤等，也可手动指定
- 📝 **输出可选** —— SRT 字幕、TXT 纯文本，单选或都要
- 💾 **断点续转** —— 已有结果自动跳过，可强制重转
- 📊 **系统监控** —— 顶栏显示 CPU / 内存 / 温度
- 📦 **三种模型** —— `large-v3`（最准）/ `medium`（推荐）/ `small`（最快）

---

## 🚀 快速开始

> ⚠️ 仓库只含源码，**模型和引擎不随仓库分发**，按下面三步准备即可。

### 1️⃣ 下载模型（Hugging Face）

**<https://huggingface.co/ggerganov/whisper.cpp/tree/main>** （按需下一个，日常推荐 medium）

| 模型（点名称直接下载） | 大小 | 适合 |
| --- | --- | --- |
| [`ggml-medium.bin`](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin) | 1.5 GB | ⭐ 日常推荐，中文效果接近 large |
| [`ggml-large-v3.bin`](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin) | 2.9 GB | 🏆 最准，较慢 |
| [`ggml-small.bin`](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin) | 466 MB | ⚡ 最快，适合草稿 |

下载后放进 `tools/whisper/`（与 `whisper-cli.exe` 同目录）。下载只需一次，之后**全程离线**。

### 2️⃣ 准备依赖

| 依赖 | 来源 | 放哪 |
| --- | --- | --- |
| Python 3 | [python.org](https://www.python.org/downloads/)，无需 `pip install` | 系统 PATH |
| `whisper-cli.exe`、`whisper-server.exe` 及 DLL | [whisper.cpp Releases](https://github.com/ggml-org/whisper.cpp/releases) | `tools/whisper/` |
| `ffmpeg.exe` | [ffmpeg 官网](https://ffmpeg.org/download.html) | `tools/` 或 PATH |

### 3️⃣ 启动

```bash
python server.py        # 自动打开浏览器；或双击 启动GUI.bat
```

界面上：**选择文件 / 选择文件夹 → 开始转写 → 打开结果文件夹**。

---

## ⌨️ 命令行（可选）

```bash
python transcribe.py "D:/录音" -o "D:/结果" -m ggml-medium.bin
python transcribe.py "会议.mp4" -l zh --prompt "张伟,腾讯云"   # 提示词更准
```

常用参数：`-m` 模型 · `-l` 语言 · `--formats srt,txt` · `--overwrite` 重转 · `--flat` 结果平铺。

---

## ❓ 常见问题

<details><summary>🖱️ 文件选择框没弹出来 / 选完没反应？</summary>

弹窗可能被挡住，会自动置顶；旧界面按 `Ctrl+F5` 刷新。
</details>

<details><summary>🛡️ 被杀毒软件误报？</summary>

`whisper-cli` / `ffmpeg` 是开源项目未签名的编译产物，偶发误报属正常。把工具目录加入信任列表即可，可对照 [whisper.cpp 官方发布](https://github.com/ggml-org/whisper.cpp/releases) 核对文件。
</details>

<details><summary>🐌 转写慢 / 内存占用大？</summary>

内存主要是模型本身（large-v3 约 4GB、medium 约 2GB）。日常用 `medium`、并行数 `1` 即可；任务结束 5 分钟后自动释放。
</details>

<details><summary>📁 结果在哪里？</summary>

在界面设置的「转写结果目录」里（默认 `转写结果/`），点「打开结果文件夹」直达。
</details>

---

## 👤 关于作者

**Jasonwei** —— 硕士在读，平时喜欢 vibe coding 一些小东西。

## 🙏 鸣谢

- **OpenAI** · **Whisper 模型**
- **小爱同学**（Xiaomi MiMo）
- **Codex**

---

## 📜 版权与许可

**Copyright © 2026 Jasonwei** · 本项目以 [MIT License](https://opensource.org/licenses/MIT) 开源发布。

- 🤖 模型（Whisper 权重）：[Hugging Face](https://huggingface.co/ggerganov/whisper.cpp)，遵循其原项目条款
- ⚙️ 引擎：[whisper.cpp](https://github.com/ggml-org/whisper.cpp) · 🎬 [ffmpeg](https://ffmpeg.org/)，本仓库不分发上述二进制与模型
