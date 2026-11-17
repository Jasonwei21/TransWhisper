#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量语音转文字（基于 whisper.cpp 的 whisper-cli.exe）

用法示例
--------
# 转单个文件（默认中文、输出 srt+txt 到 ./output）
python transcribe.py "D:/audio/会议.mp4"

# 转整个文件夹（含子目录），指定输出目录，用 medium 模型快一点
python transcribe.py "D:/audio" -o "D:/转写结果" -m ggml-medium.bin

# 中文 + 提示词提升专有名词准确率 + 8 进程并行
python transcribe.py "D:/audio" -l zh --prompt "腾讯云,企业微信,张伟" -w 4

# 输出 lrc 歌词 / vtt 字幕
python transcribe.py "D:/audio" --formats srt,txt,lrc
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

# ============ 路径与默认配置（便携式，跟随脚本所在目录） ============
BASE_DIR = Path(__file__).resolve().parent

# 转写结果永远只写这一个目录，其他目录一概不算数
RESULTS_DIR = BASE_DIR / "转写结果"

MODEL = "ggml-large-v3.bin"
LANGUAGE = "auto"     # 自动检测语言；已知语言时填 zh/en/ja 等会更快更稳
THREADS = 0          # 0 = 自动（物理核心的 75%）
WORKERS = 1          # 并行进程数；每个进程约占 3~4GB 内存，别贪多


def _find_whisper_dir() -> Path:
    """查找 whisper 程序目录：环境变量 > 便携版目录 > 本机旧位置。"""
    env = os.environ.get("ASR_WHISPER_DIR")
    if env:
        return Path(env)
    candidates = [
        BASE_DIR / "tools" / "whisper",        # 便携版布局
        BASE_DIR / "whisper",
        Path(r"D:\AI烤肉\_internal\whisper"),   # 本机原始位置（开发用）
    ]
    for c in candidates:
        if (c / "whisper-cli.exe").is_file():
            return c
    return candidates[0]


def _find_ffmpeg() -> Path:
    """查找 ffmpeg：环境变量 > 便携版目录 > 本机旧位置 > PATH。"""
    env = os.environ.get("ASR_FFMPEG")
    if env:
        return Path(env)
    candidates = [
        BASE_DIR / "tools" / "ffmpeg.exe",
        BASE_DIR / "ffmpeg.exe",
        Path(r"D:\AI烤肉\_internal\ffmpeg.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("ffmpeg")
    return Path(found) if found else candidates[0]


WHISPER_DIR = _find_whisper_dir()
FFMPEG = _find_ffmpeg()
OUTPUT_DIR = RESULTS_DIR  # 兼容旧引用：结果目录固定
# ===================================================================

MEDIA_EXTS = {
    # 音频
    ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".amr", ".aiff",
    # 视频
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".ts", ".m4v", ".mpg", ".mpeg",
}

# 支持的输出格式 -> whisper-cli 参数
FORMAT_FLAGS = {
    "srt": "-osrt", "vtt": "-ovtt", "txt": "-otxt",
    "lrc": "-olrc", "csv": "-ocsv", "json": "-oj", "jsonfull": "-ojf",
}


def die(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(1)


# ================= 系统资源相关 =================

_SYSINFO: dict | None = None


def _win_physical_cores() -> int | None:
    """物理核心数（GetLogicalProcessorInformation，纯系统 API）。"""
    try:
        import ctypes

        class SLPI(ctypes.Structure):
            _fields_ = [("ProcessorMask", ctypes.c_size_t),
                        ("Relationship", ctypes.c_int),
                        ("Data", ctypes.c_byte * 16)]

        RelationProcessorCore = 0
        size = ctypes.c_size_t(0)
        ctypes.windll.kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
        if not size.value:
            return None
        count = size.value // ctypes.sizeof(SLPI)
        buf = (SLPI * count)()
        if not ctypes.windll.kernel32.GetLogicalProcessorInformation(buf, ctypes.byref(size)):
            return None
        return sum(1 for i in range(count) if buf[i].Relationship == RelationProcessorCore)
    except Exception:  # noqa: BLE001
        return None


def _win_ram_gb() -> float | None:
    """物理内存 GB（GlobalMemoryStatusEx）。"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(ms)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        return round(ms.ullTotalPhys / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        return None


def _win_gpus() -> list[str]:
    """显卡名称列表（EnumDisplayDevicesA）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DISPLAY_DEVICEA(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("DeviceName", ctypes.c_char * 32),
                ("DeviceString", ctypes.c_char * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", ctypes.c_char * 128), ("DeviceKey", ctypes.c_char * 128),
            ]

        gpus = []
        i = 0
        while True:
            dev = DISPLAY_DEVICEA()
            dev.cb = ctypes.sizeof(dev)
            if not ctypes.windll.user32.EnumDisplayDevicesA(None, i, ctypes.byref(dev), 0):
                break
            name = dev.DeviceString.decode("gbk", errors="replace")
            if name and name not in gpus:  # 同一适配器会按输出口重复列出，去重
                gpus.append(name)
            i += 1
        return gpus
    except Exception:  # noqa: BLE001
        return []


def system_info() -> dict:
    """收集 CPU / 内存 / 显卡信息（纯系统 API，不调用 PowerShell）。"""
    global _SYSINFO
    if _SYSINFO is not None:
        return _SYSINFO

    info = {
        "logical": os.cpu_count() or 4,
        "physical": _win_physical_cores(),
        "ram_gb": _win_ram_gb(),
        "gpus": _win_gpus(),
        "gpu_recommended": True,
        "gpu_note": "",
    }

    # Intel 核显/集显跑 Vulkan 推理会与屏幕显示抢资源，鼠标会明显卡顿
    real = [g for g in info["gpus"] if "virtual" not in g.lower()]
    if any("intel" in g.lower() for g in real) and not any(
        "nvidia" in g.lower() or "geforce" in g.lower() for g in real
    ):
        info["gpu_recommended"] = False
        info["gpu_note"] = (
            "检测到 Intel 显卡：GPU 推理会和屏幕显示抢资源（鼠标卡顿），"
            "已默认关闭 GPU 加速。如需更快可在高级设置里手动开启。"
        )

    _SYSINFO = info
    return info


def default_threads() -> int:
    """默认线程数：物理核心的 75%，既快又给系统留出余量，避免 UI 卡死。"""
    info = system_info()
    phys = info.get("physical") or info.get("logical") or 4
    return max(2, int(round(phys * 0.75)))


def _spawn_kwargs() -> dict:
    """推理/转码进程用低于正常优先级：空闲时全速跑，动鼠标时自动让路。"""
    if os.name == "nt":
        return {"creationflags": 0x00004000}  # BELOW_NORMAL_PRIORITY_CLASS
    return {}


# 供 GUI 等外部调用方中断：记录当前正在运行的 whisper 子进程
_ACTIVE_PROCS: set = set()


def stop_active_procs() -> None:
    """杀掉所有正在运行的 whisper-cli 进程（用于停止当前任务）。"""
    for proc in list(_ACTIVE_PROCS):
        try:
            proc.kill()
        except OSError:
            pass


@contextmanager
def ascii_tempdir():
    """创建一个纯 ASCII 路径的临时目录。

    whisper-cli.exe 用 ANSI 代码页解析命令行参数，一旦参数里出现中文路径
    （如 D:\\AI烤肉\\...）就会直接崩溃，所以传给它的路径必须全是 ASCII。
    """
    candidates = [Path(tempfile.gettempdir()), Path("C:/Windows/Temp"), Path("C:/Temp")]
    root = next((p for p in candidates if str(p).isascii() and p.is_dir()), None)
    if root is None:
        root = Path(tempfile.gettempdir())
        print(f"[警告] 临时目录含非 ASCII 字符（{root}），中文路径可能仍会失败", file=sys.stderr)
    d = Path(tempfile.mkdtemp(prefix="asr_", dir=root))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_env(model: str, ffmpeg: Path) -> tuple[Path, Path, Path]:
    """校验 whisper-cli / 模型 / ffmpeg 是否齐备，返回三个路径。"""
    if not WHISPER_DIR.is_dir():
        die(f"找不到 whisper 目录：{WHISPER_DIR}\n请修改脚本顶部的 WHISPER_DIR")
    cli = WHISPER_DIR / "whisper-cli.exe"
    if not cli.is_file():
        die(f"找不到 {cli}")

    model_path = Path(model)
    if not model_path.is_absolute():
        model_path = WHISPER_DIR / model
    if not model_path.is_file():
        die(f"找不到模型文件：{model_path}\n可选：{', '.join(p.name for p in WHISPER_DIR.glob('ggml-*.bin'))}")

    if not ffmpeg.is_file():
        alt = shutil.which("ffmpeg")
        if alt:
            ffmpeg = Path(alt)
        else:
            die(f"找不到 ffmpeg：{ffmpeg}\n请修改脚本顶部的 FFMPEG 或把 ffmpeg 加入 PATH")

    # 模型在同目录时只传文件名，避免中文目录拼进命令行参数
    if model_path.parent.resolve() == WHISPER_DIR.resolve():
        model_arg = model_path.name
    else:
        model_arg = str(model_path)
        if not model_arg.isascii():
            print(f"[警告] 模型路径含非 ASCII 字符，whisper-cli 可能无法加载：{model_arg}",
                  file=sys.stderr)
    return cli, model_path, ffmpeg, model_arg


# 回收站/系统目录：递归扫描时必须排除，
# 否则选了磁盘根目录就会把已删除的音视频（$RECYCLE.BIN）也转写一遍
SYSTEM_DIRS = {
    "$recycle.bin", "recycler", "recycled", "system volume information",
    "recovery", "$windows.~ws", "$windows.~bt", "windows.old",
    "config.msi", "found.000",
}


def _skip_dir_name(name: str) -> bool:
    """是否跳过该目录：回收站、系统目录、隐藏目录、工具自身内部目录。"""
    if name.lower() in SYSTEM_DIRS or name.startswith("$") or name.startswith("."):
        return True
    return name.lower() in {"_uploads", "__pycache__", "_engine_cpu"}


def _in_system_path(p: Path) -> bool:
    """路径本身（或其任一上级）位于回收站/系统目录内。"""
    return any(part.lower() in SYSTEM_DIRS for part in p.parts)


def collect_media(targets: list[Path]) -> list[Path]:
    """展开输入（文件/目录），过滤出支持的音视频，按路径去重排序。"""
    found: list[Path] = []
    for t in targets:
        # 输入本身就在回收站/系统目录里的，整条跳过（否则会把删除的文件转写一遍）
        if _in_system_path(t):
            print(f"[跳过] 位于回收站/系统目录：{t}")
            continue
        if t.is_dir():
            # os.walk 可以剪枝，不会进入回收站/系统目录（rglob 会白扫一遍）
            for root, dirnames, filenames in os.walk(t):
                dirnames[:] = sorted(d for d in dirnames if not _skip_dir_name(d))
                for fn in sorted(filenames):
                    p = Path(root) / fn
                    if p.suffix.lower() in MEDIA_EXTS:
                        found.append(p)
        elif t.is_file():
            if t.suffix.lower() in MEDIA_EXTS:
                found.append(t)
            else:
                print(f"[跳过] 不支持的格式：{t.name}")
        else:
            print(f"[跳过] 路径不存在：{t}")

    seen, uniq = set(), []
    for p in found:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def to_wav(src: Path, dst: Path, ffmpeg: Path) -> None:
    """转成 whisper 要求的 16kHz / 单声道 / 16bit PCM WAV。"""
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(src),
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        "-y", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **_spawn_kwargs())
    if r.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1024:
        raise RuntimeError(f"ffmpeg 转码失败：{(r.stderr or '').strip()[:300]}")


def run_whisper(cli: Path, model_arg: str, wav: Path, out_base: Path,
                lang: str, threads: int, formats: list[str],
                prompt: str | None, translate: bool,
                no_gpu: bool, flash_attn: bool) -> None:
    cmd = [str(cli), "-m", model_arg, "-f", str(wav), "-l", lang,
           "-t", str(threads), "-of", str(out_base)]
    for f in formats:
        cmd.append(FORMAT_FLAGS[f])
    if prompt:
        cmd += ["--prompt", prompt]
    if translate:
        cmd.append("-tr")
    if no_gpu:
        cmd.append("-ng")
    if flash_attn:
        cmd.append("-fa")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(WHISPER_DIR), **_spawn_kwargs())
    _ACTIVE_PROCS.add(proc)
    try:
        out, err = proc.communicate()
    finally:
        _ACTIVE_PROCS.discard(proc)
    if proc.returncode != 0:
        tail = (err or out or "").strip().splitlines()
        raise RuntimeError(f"whisper-cli 返回码 {proc.returncode}：{' / '.join(tail[-3:])[:300]}")


def process_one(src: Path, args, ctx) -> tuple[Path, float, str | None]:
    """处理单个文件，返回 (源文件, 耗时秒, 错误信息或 None)。"""
    cli, model_path, ffmpeg, model_arg, threads = ctx
    t0 = time.perf_counter()

    out_dir: Path = args.output
    out_dir.mkdir(parents=True, exist_ok=True)

    # 输出目录：默认保留子目录结构，避免不同文件夹里的同名文件互相覆盖
    try:
        rel = src.relative_to(args.base_dir).parent if args.base_dir else Path()
    except ValueError:
        rel = Path()
    target_dir = (out_dir / rel) if args.keep_structure else out_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    if not args.overwrite and all(
        (target_dir / f"{src.stem}.{f}").exists() for f in args.formats
    ):
        return src, 0.0, "SKIP"

    # 关键：所有传给 whisper-cli 的路径都放在纯 ASCII 临时目录里
    with ascii_tempdir() as tmp:
        wav = tmp / "a.wav"
        to_wav(src, wav, ffmpeg)

        tmp_base = tmp / "out"
        run_whisper(cli, model_arg, wav, tmp_base, args.language, threads,
                    args.formats, args.prompt, args.translate,
                    args.no_gpu, args.flash_attn)

        moved = 0
        for f in args.formats:
            produced = tmp_base.with_name(f"out.{f}")
            if not produced.is_file():
                continue
            dst = target_dir / f"{src.stem}.{f}"
            dst.unlink(missing_ok=True)
            shutil.move(str(produced), str(dst))
            moved += 1
        if moved == 0:
            raise RuntimeError("whisper-cli 未生成任何结果文件（检查音频是否有声音）")

    return src, time.perf_counter() - t0, None


def main() -> None:
    p = argparse.ArgumentParser(
        description="批量语音转文字（whisper.cpp / ggml 模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", type=Path, help="音频/视频文件或文件夹，可多个")
    p.add_argument("-o", "--output", type=Path, default=OUTPUT_DIR, help=f"输出目录（默认 {OUTPUT_DIR}）")
    p.add_argument("-m", "--model", default=MODEL,
                   help=f"模型文件名或绝对路径（默认 {MODEL}），可选 ggml-medium.bin / ggml-small.bin")
    p.add_argument("-l", "--language", default=LANGUAGE,
                   help=f"语言代码，如 zh / en / ja / auto（默认 {LANGUAGE}）")
    p.add_argument("-t", "--threads", type=int, default=THREADS,
                   help="每进程线程数，0=自动（物理核心的 75%%，避免卡死系统）")
    p.add_argument("-w", "--workers", type=int, default=WORKERS,
                   help=f"并行处理的文件数（默认 {WORKERS}）；large 模型每进程约 3~4GB 内存")
    p.add_argument("--formats", default="srt,txt",
                   help="输出格式，逗号分隔：srt,vtt,txt,lrc,csv,json,jsonfull（默认 srt,txt）")
    p.add_argument("--prompt", default=None, help="初始提示词，用于指定专有名词/术语，提升识别准确率")
    p.add_argument("--translate", action="store_true", help="翻译成英文输出")
    p.add_argument("--no-gpu", action="store_true", help="禁用 GPU（Vulkan），强制 CPU 推理")
    p.add_argument("--flash-attn", action="store_true", help="开启 flash attention（一般更快更省内存）")
    p.add_argument("--overwrite", action="store_true", help="已存在结果也重新转写（默认跳过，便于断点续跑）")
    p.add_argument("--flat", dest="keep_structure", action="store_false",
                   help="所有结果平铺到输出目录，不保留子目录结构")
    args = p.parse_args()

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in FORMAT_FLAGS]
    if bad:
        die(f"不支持的输出格式：{', '.join(bad)}；可选 {', '.join(FORMAT_FLAGS)}")
    if not formats:
        formats = ["srt", "txt"]
    args.formats = formats

    cli, model, ffmpeg, model_arg = check_env(args.model, FFMPEG)

    threads = args.threads or default_threads()
    files = collect_media(args.inputs)
    if not files:
        die("没有找到可处理的音视频文件")

    args.base_dir = None
    if len(args.inputs) == 1 and args.inputs[0].is_dir():
        args.base_dir = args.inputs[0]

    print(f"whisper  : {cli}")
    print(f"模型     : {model.name}  ({model.stat().st_size / 1024**3:.2f} GB)")
    print(f"ffmpeg   : {ffmpeg}")
    print(f"语言     : {args.language}   线程: {threads}   并行: {args.workers}")
    print(f"输出     : {args.output.resolve()}   格式: {', '.join(formats)}")
    print(f"待处理   : {len(files)} 个文件\n")

    ctx = (cli, model, ffmpeg, model_arg, threads)
    ok = skip = fail = 0
    t_all = time.perf_counter()

    def report(i: int, total: int, src: Path, elapsed: float, err: str | None) -> None:
        nonlocal ok, skip, fail
        tag = f"[{i}/{total}] {src.name}"
        if err == "SKIP":
            skip += 1
            print(f"{tag} —— 已存在，跳过")
        elif err:
            fail += 1
            print(f"{tag} —— 失败：{err}")
        else:
            ok += 1
            print(f"{tag} —— 完成，用时 {elapsed:.1f}s")

    if args.workers <= 1:
        for i, src in enumerate(files, 1):
            try:
                _, el, err = process_one(src, args, ctx)
            except Exception as e:  # noqa: BLE001
                el, err = 0.0, str(e)
            report(i, len(files), src, el, err)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(process_one, s, args, ctx): s for s in files}
            for i, fut in enumerate(as_completed(futs), 1):
                src = futs[fut]
                try:
                    _, el, err = fut.result()
                except Exception as e:  # noqa: BLE001
                    el, err = 0.0, str(e)
                report(i, len(files), src, el, err)

    print(f"\n全部结束：成功 {ok}，跳过 {skip}，失败 {fail}，"
          f"总耗时 {time.perf_counter() - t_all:.1f}s")
    print(f"结果目录：{args.output.resolve()}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
