#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包「语音转文字」便携版 —— 生成一个拷到任何 Windows 电脑都能用的文件夹

用法
----
python 制作便携版.py                          # 输出到 ./便携版，包含全部模型
python 制作便携版.py --out D:\\语音转文字       # 指定输出位置
python 制作便携版.py --models medium,small     # 只带需要的模型（省空间）
python 制作便携版.py --no-python               # 不带 Python（目标机器已装）

产物结构
--------
语音转文字(便携版)/
├── 启动GUI.bat        双击即用
├── server.py          服务端
├── transcribe.py      转写核心 + 命令行入口
├── filepick.py        Windows 原生文件选择框
├── web/               图形界面
├── 转写结果/          默认结果目录（可在界面里改）
├── tools/
│   ├── ffmpeg.exe     转码工具
│   ├── whisper/       whisper-cli / whisper-server + 模型
│   └── whisper-cpu/   不含 Vulkan 的 CPU-only 引擎副本
├── python/            自带 Python（目标机器无需安装）
├── 校验值-SHA256.txt  引擎文件哈希清单
├── README.txt / 更新日志.txt / 使用说明.txt
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import transcribe  # noqa: E402

MODEL_ALIAS = {
    "large-v3": "ggml-large-v3.bin",
    "medium": "ggml-medium.bin",
    "small": "ggml-small.bin",
}

# whisper-cli/whisper-server 运行所需的程序文件（不含 bench 等无关 exe）
WHISPER_EXE = ["whisper-cli.exe", "whisper-server.exe", "quantize.exe"]

README = """语音转文字（便携版）
====================

怎么用
------
1. 双击「启动GUI.bat」，浏览器会自动打开图形界面
2. 点「＋ 选择文件」或「＋ 选择文件夹」，在弹出的
   Windows 自带选择框里选好文件（选择框会自动置顶）
3. 点「开始转写」
4. 完成后点「打开结果文件夹」查看 .srt / .txt

说明
----
- 批量转写请用「选择文件夹」；不支持拖拽上传
- 工具直接引用本机原文件，不复制、不上传
- 结果目录可在界面里改，设置保存在 config.json
- 完整文档见同目录 README.txt 与 更新日志.txt

关于本机
--------
- 不需要安装 Python：自带 python 目录
- 不需要联网：模型和引擎全在本地，文件不上传
- 系统要求：Windows 10 / 11，64 位

常见问题
--------
- 提示找不到 Python：确认 python 文件夹和 启动GUI.bat 在同一层
- 浏览器没自动打开：看黑色命令行窗口里显示的真实地址
- 选完文件列表没反应：按 Ctrl+F5 刷新浏览器（v14 已修复该问题）
- 杀毒软件误报：属误报，把整个文件夹加入白名单；
  可用「校验值-SHA256.txt」核对引擎文件哈希
- 想换模型 / 语言 / 输出格式：界面里直接选
"""


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  + {dst.relative_to(dst.parents[1]) if dst.parents[1] != dst else dst.name}"
          f"  ({src.stat().st_size / 1024 / 1024:.1f}MB)")


def _copy_cpu_engine(out: Path) -> None:
    """预置不含 ggml-vulkan.dll 的引擎副本（禁用 GPU 时用），运行时零复制。"""
    cpu_dir = out / "tools" / "whisper-cpu"
    cpu_dir.mkdir(parents=True, exist_ok=True)
    src_files = [transcribe.WHISPER_DIR / "whisper-server.exe"] + \
        sorted(transcribe.WHISPER_DIR.glob("*.dll"))
    for f in src_files:
        if f.name.lower() == "ggml-vulkan.dll":
            continue
        target = cpu_dir / f.name
        if not target.exists() or target.stat().st_size != f.stat().st_size:
            shutil.copy2(f, target)
    print(f"  + tools\\whisper-cpu\\  (共 {len(list(cpu_dir.iterdir()))} 个文件)")


def write_manifest(out: Path) -> None:
    """生成引擎文件 SHA256 校验值清单，供杀毒误报时与官方发布核对。"""
    import hashlib
    lines = [
        "引擎文件 SHA256 校验值",
        "=" * 52,
        "用途：若杀毒软件误报，可用本清单核对文件是否被篡改。",
        "whisper 相关文件来自 whisper.cpp 官方发布，ffmpeg 来自官方构建。",
        "",
    ]
    count = 0
    for f in sorted((out / "tools").rglob("*")):
        if f.is_file() and f.suffix.lower() in {".exe", ".dll"}:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{digest}  {f.relative_to(out)}")
            count += 1
    (out / "校验值-SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  + 校验值-SHA256.txt  ({count} 个文件)")


def main() -> None:
    ap = argparse.ArgumentParser(description="打包语音转文字便携版")
    ap.add_argument("--out", type=Path, default=BASE_DIR / "便携版",
                    help="输出目录（默认 ./便携版）")
    ap.add_argument("--models", default="all",
                    help="要打包的模型，逗号分隔：large-v3,medium,small 或 all（默认 all）")
    ap.add_argument("--no-python", action="store_true",
                    help="不打包自带 Python（目标机器已安装 Python 时）")
    ap.add_argument("--only-code", action="store_true",
                    help="只更新程序代码（不重拷引擎/模型/Python，用于便携版升级）")
    args = ap.parse_args()

    out: Path = args.out
    out = out.resolve()
    if out == BASE_DIR:
        sys.exit("[错误] 输出目录不能是当前工具目录本身")

    # ---- 校验来源 ----
    cli = transcribe.WHISPER_DIR / "whisper-cli.exe"
    if not cli.is_file():
        sys.exit(f"[错误] 找不到 whisper-cli.exe：{transcribe.WHISPER_DIR}")
    ffmpeg = transcribe.FFMPEG
    if not ffmpeg.is_file():
        sys.exit(f"[错误] 找不到 ffmpeg：{ffmpeg}")

    # 选择模型
    if args.models.strip().lower() == "all":
        wanted = list(MODEL_ALIAS.values())
    else:
        wanted = []
        for m in args.models.split(","):
            m = m.strip()
            name = MODEL_ALIAS.get(m, m)
            if not (transcribe.WHISPER_DIR / name).is_file():
                sys.exit(f"[错误] 模型不存在：{name}（可选：{', '.join(MODEL_ALIAS)}）")
            wanted.append(name)

    total_model_gb = sum((transcribe.WHISPER_DIR / m).stat().st_size for m in wanted) / 1024**3
    print(f"输出目录 : {out}")
    print(f"模型     : {', '.join(wanted)}  共 {total_model_gb:.2f} GB")
    if out.exists() and any(out.iterdir()):
        print(f"[注意] 输出目录已存在且非空，同名文件会被覆盖：{out}")
    print()

    # ---- 1. 程序脚本 ----
    print("[1/5] 拷贝程序脚本")
    for name in ("server.py", "transcribe.py", "filepick.py", "启动GUI.bat"):
        copy_file(BASE_DIR / name, out / name)
    for f in sorted((BASE_DIR / "web").iterdir()):
        if f.is_file():
            copy_file(f, out / "web" / f.name)
        elif f.is_dir():   # 如 web/vendor/（Vue 运行时等本地依赖）
            shutil.copytree(f, out / "web" / f.name, dirs_exist_ok=True)

    if args.only_code:
        (out / "转写结果").mkdir(exist_ok=True)
        (out / "使用说明.txt").write_text(README, encoding="utf-8")
        for extra in ("README.md", "README.txt", "更新日志.txt"):
            src = BASE_DIR / extra
            if src.is_file():
                shutil.copy2(src, out / extra)
        if not (out / "tools" / "whisper-cpu" / "whisper-server.exe").is_file():
            _copy_cpu_engine(out)
        write_manifest(out)
        print("\n（--only-code）代码更新完成，引擎/模型/Python 保持不动")
        return

    # ---- 2. whisper 引擎 ----
    print("[2/5] 拷贝 whisper 引擎")
    for name in WHISPER_EXE:
        copy_file(transcribe.WHISPER_DIR / name, out / "tools" / "whisper" / name)
    for dll in sorted(transcribe.WHISPER_DIR.glob("*.dll")):
        copy_file(dll, out / "tools" / "whisper" / dll.name)

    # CPU-only 引擎副本（预置到 tools\whisper-cpu，运行时只执行、不复制，
    # 避免"复制并执行 exe"触发杀毒软件启发式）
    print("     预置 CPU-only 引擎副本")
    _copy_cpu_engine(out)

    # ---- 3. 模型 ----
    print("[3/5] 拷贝模型")
    for name in wanted:
        copy_file(transcribe.WHISPER_DIR / name, out / "tools" / "whisper" / name)

    # ---- 4. ffmpeg ----
    print("[4/5] 拷贝 ffmpeg")
    copy_file(ffmpeg, out / "tools" / "ffmpeg.exe")

    # ---- 5. 便携 Python ----
    print("[5/5] 拷贝 Python 运行环境")
    if args.no_python:
        print("  （已跳过，目标机器需自备 Python 3）")
    else:
        py_src = None
        for cand in (
            Path.home() / ".workbuddy/binaries/python/versions",
            Path(r"C:\Python313"),
        ):
            if cand.name == "versions" and cand.is_dir():
                versions = sorted(p for p in cand.iterdir() if (p / "python.exe").is_file())
                if versions:
                    py_src = versions[-1]
                    break
            elif (cand / "python.exe").is_file():
                py_src = cand
                break
        if py_src is None:
            print("  [警告] 没找到可打包的 Python，目标机器需自备 Python 3")
        else:
            py_dst = out / "python"
            if py_dst.exists():
                shutil.rmtree(py_dst)
            print(f"  正在拷贝 Python（{py_src.name}，约 50MB，请稍候）…")
            shutil.copytree(py_src, py_dst, symlinks=True)
            if not (py_dst / "python.exe").is_file():
                print("  [警告] Python 拷贝不完整，建议用 --no-python 并在目标机器安装 Python")
            else:
                print(f"  + python\\python.exe  (来自 {py_src})")

    # ---- 收尾 ----
    (out / "转写结果").mkdir(exist_ok=True)
    (out / "使用说明.txt").write_text(README, encoding="utf-8")
    for extra in ("README.txt", "更新日志.txt"):
        src = BASE_DIR / extra
        if src.is_file():
            shutil.copy2(src, out / extra)
    write_manifest(out)

    size_gb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1024**3
    print()
    print("=" * 52)
    print(f"便携版打包完成：{out}")
    print(f"总大小：{size_gb:.2f} GB")
    print("把整个文件夹拷到U盘/其他电脑，双击 启动GUI.bat 即可使用")
    print("=" * 52)


if __name__ == "__main__":
    main()
