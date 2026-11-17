#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音转文字 —— 本地 Web GUI 服务端（仅依赖 Python 标准库）

启动方式：双击 `启动GUI.bat`，或运行 `python server.py`
浏览器会自动打开 http://127.0.0.1:8765

安全说明：服务只监听 127.0.0.1（本机），不对外网开放。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
VERSION = "1.0.0"              # 语义化版本号（/api/health 的 version 字段）
CONFIG_FILE = BASE_DIR / "config.json"   # 用户配置（结果目录等）
sys.path.insert(0, str(BASE_DIR))

import transcribe  # noqa: E402

# ---------------------------------------------------------------- 用户配置


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def results_dir() -> Path:
    """当前生效的结果目录（可自定义，默认工具目录下的「转写结果」）。"""
    custom = load_config().get("results_dir")
    if custom:
        p = Path(custom).expanduser()
        if p.is_absolute():
            return p
    return transcribe.RESULTS_DIR

# ---------------------------------------------------------------- 任务状态

JOB_LOCK = threading.Lock()


class Job:
    def __init__(self) -> None:
        self.state = "idle"           # idle | running | done | error | cancelled
        self.files: list[dict] = []   # {name, status, elapsed, error}
        self.log: deque[str] = deque(maxlen=800)
        self.output_dir: Path | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with JOB_LOCK:
            done = sum(1 for f in self.files if f["status"] in ("done", "skip", "fail"))
            return {
                "state": self.state,
                "files": [dict(f) for f in self.files],
                "log": list(self.log)[-300:],
                "output_dir": str(self.output_dir) if self.output_dir else None,
                "total": len(self.files),
                "done": done,
                "metrics": METRICS.snapshot() if METRICS else None,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def log_line(self, text: str) -> None:
        with JOB_LOCK:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {text}")


JOB = Job()


# ---------------------------------------------------------------- 系统监控

class MetricsSampler:
    """后台采样 CPU/内存/温度，接口只读缓存，不在请求里阻塞。

    - CPU/内存：Windows API（ctypes），几乎零开销，2 秒一采
    - 温度：typeperf 读 Windows 性能计数器（Thermal Zone），15 秒一采；GPU 温度多数机型不开放
    """

    # 温度采集用系统自带 typeperf（微软签名程序），不调用 PowerShell ——
    # 频繁拉起 powershell.exe 是杀软启发式的重点监控对象，必须避免
    COUNTER = r"\Thermal Zone Information(*)\Temperature"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {"cpu_pct": None, "mem_pct": None,
                            "cpu_temp": None, "gpu_temp": None}
        self._prev: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_temp = 0.0
        while not self._stop.is_set():
            try:
                self._sample_fast()
                if time.time() - last_temp >= 15:
                    self._sample_temps()
                    last_temp = time.time()
            except Exception:  # noqa: BLE001 监控失败不影响主功能
                pass
            self._stop.wait(2)

    def _sample_fast(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return
        u64 = lambda f: (f.hi << 32) | f.lo  # noqa: E731
        cur = (u64(idle), u64(kernel), u64(user))

        cpu = None
        if self._prev is not None:
            total = (cur[1] - self._prev[1]) + (cur[2] - self._prev[2])
            busy = total - (cur[0] - self._prev[0])
            if total > 0:
                cpu = round(100.0 * busy / total, 1)
        self._prev = cur

        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(ms)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))

        with self._lock:
            if cpu is not None:
                self._data["cpu_pct"] = cpu
            self._data["mem_pct"] = int(ms.dwMemoryLoad)

    def _sample_temps(self) -> None:
        try:
            out = subprocess.run(
                ["typeperf", self.COUNTER, "-sc", "1"],
                capture_output=True, text=True, timeout=15,
                creationflags=0x08000000)  # CREATE_NO_WINDOW
        except Exception:  # noqa: BLE001
            return
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return
        # typeperf 输出 PDH-CSV（数值单位开尔文），但尾部可能混入提示文字，
        # 所以不取固定行，而是找最后一条能解析出数字的行
        rows = [[p.strip().strip('"') for p in ln.split('","')] for ln in lines]
        header = rows[0]
        data = None
        for parts in rows[1:]:
            if len(parts) >= 2:
                try:
                    float(parts[1])
                    data = parts
                except ValueError:
                    continue
        if not data:
            return
        zones: dict[str, float] = {}
        for hdr, val in zip(header[1:], data[1:]):
            try:
                zones[hdr.lower()] = float(val) - 273.15
            except ValueError:
                continue
        if not zones:
            return
        cpu_t = round(max(zones.values()), 1)
        gpu_t = next((round(v, 1) for k, v in zones.items() if "gpu" in k), None)
        with self._lock:
            self._data["cpu_temp"] = cpu_t
            self._data["gpu_temp"] = gpu_t

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


METRICS = MetricsSampler()


# ---------------------------------------------------------------- 工具函数

def list_models() -> list[dict]:
    out = []
    for p in sorted(transcribe.WHISPER_DIR.glob("ggml-*.bin")):
        out.append({"name": p.name, "size_gb": round(p.stat().st_size / 1024**3, 2)})
    return out


# 转写产物的合法扩展名——结果面板只列这些，
# 避免输出目录里混有 dll/音视频等其它文件时被误当成转写结果
RESULT_EXTS = {".srt", ".vtt", ".txt", ".lrc", ".csv", ".json"}


def list_results(out_dir: Path) -> list[dict]:
    results = []
    if not out_dir.is_dir() or transcribe._in_system_path(out_dir):
        return results
    # 用 os.walk 剪枝，绝不递归进回收站/系统目录（否则会把删除的文件列进结果）
    for root, dirnames, filenames in os.walk(out_dir):
        dirnames[:] = sorted(d for d in dirnames if not transcribe._skip_dir_name(d))
        for fn in sorted(filenames):
            f = Path(root) / fn
            if f.suffix.lower() not in RESULT_EXTS:
                continue
            try:
                st = f.stat()
                results.append({
                    "name": f.name,
                    "path": str(f),
                    "rel": str(f.relative_to(out_dir)),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
            except OSError:
                continue
    return results


# ---------------------------------------------------------------- 任务执行

def build_args(payload: dict):
    ns = argparse.Namespace()
    # 结果只写进当前配置的结果目录，其他位置一概不算数
    ns.output = results_dir()
    ns.language = payload.get("language") or transcribe.LANGUAGE
    # 输出格式只支持 SRT / TXT：可任选其一，或两个都要
    chosen = [f for f in (payload.get("formats") or []) if f in ("srt", "txt")]
    ns.formats = chosen or ["srt", "txt"]
    ns.prompt = payload.get("prompt") or None
    ns.translate = bool(payload.get("translate"))
    ns.no_gpu = bool(payload.get("no_gpu"))
    ns.flash_attn = bool(payload.get("flash_attn"))
    ns.overwrite = bool(payload.get("overwrite"))
    ns.keep_structure = not bool(payload.get("flat"))
    ns.base_dir = None
    return ns


# ---------------------------------------------------------------- 常驻推理引擎
#
# 每个文件都用 whisper-cli 的话，3GB 模型要反复从磁盘加载。
# 这里常驻一个 whisper-server 子进程：模型只加载一次，逐文件 HTTP 推理。

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stage_cpu_engine() -> Path:
    """CPU-only 引擎目录（不含 ggml-vulkan.dll，只能 CPU 推理）。

    whisper-server 没有 -ng 参数，Vulkan dll 在旁边就会用 GPU；
    排除掉它，引擎只能走 CPU。

    重要：优先使用打包时预置的目录（tools/whisper-cpu、_engine_cpu），
    运行时只「执行已有文件」，不做任何复制 —— 复制并执行 exe 是
    杀毒软件启发式重点盯防的行为，必须避免。
    """
    for prebuilt in (BASE_DIR / "tools" / "whisper-cpu", BASE_DIR / "_engine_cpu"):
        if (prebuilt / "whisper-server.exe").is_file():
            return prebuilt
    # 兜底：极少数情况下预置目录缺失，补齐一次（仅首次，不会反复复制）
    dst = BASE_DIR / "_engine_cpu"
    dst.mkdir(exist_ok=True)
    src = transcribe.WHISPER_DIR
    files = [src / "whisper-server.exe"] + sorted(src.glob("*.dll"))
    for f in files:
        if f.name.lower() == "ggml-vulkan.dll":
            continue
        target = dst / f.name
        if not target.exists() or target.stat().st_size != f.stat().st_size:
            shutil.copy2(f, target)
    return dst


class WhisperBackend:
    """whisper-server 子进程管理：常驻、按需换模型、空闲自动退出。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.key = ""           # 当前实例的配置指纹
        self.lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None

    def ensure(self, model_name: str, threads: int,
               no_gpu: bool, flash_attn: bool) -> None:
        key = f"{model_name}|{threads}|{no_gpu}|{flash_attn}"
        with self.lock:
            if self.proc and self.proc.poll() is None and self.key == key:
                self._cancel_idle()
                return
            self._stop_locked()
            exe_dir = transcribe.WHISPER_DIR
            cmd = [str(exe_dir / "whisper-server.exe"), "-m", model_name,
                   "-t", str(threads), "--host", "127.0.0.1", "--port", str(self.port or _free_port())]
            if flash_attn:
                cmd.append("-fa")
            if no_gpu:
                # 用不带 Vulkan dll 的引擎副本 → 只能 CPU 推理
                staged = _stage_cpu_engine()
                cmd[0] = str(staged / "whisper-server.exe")
            self.port = _free_port()
            cmd[-1] = str(self.port)
            self.proc = subprocess.Popen(
                cmd, cwd=str(transcribe.WHISPER_DIR),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **transcribe._spawn_kwargs())
            self.key = key
            self._wait_ready()

    def _wait_ready(self, timeout: float = 180.0) -> None:
        import http.client
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server 进程启动即退出")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                conn.request("GET", "/")
                conn.getresponse()
                conn.close()
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("whisper-server 启动超时")

    def infer(self, wav: Path, language: str | None,
              prompt: str | None, translate: bool) -> dict:
        """单文件推理，返回 verbose_json（含 text + segments）。"""
        import http.client
        fields = {"response_format": "verbose_json", "temperature": "0.0"}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt
        if translate:
            fields["translate"] = "true"

        boundary = f"----asr{time.time_ns()}"
        chunks = []
        for k, v in fields.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{wav.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode())
        chunks.append(wav.read_bytes())
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(chunks)

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=7200)
        try:
            conn.request("POST", "/inference", body=body,
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()
        if status != 200:
            raise RuntimeError(f"推理服务返回 HTTP {status}")
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))
        return data

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._cancel_idle()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None
        self.key = ""

    def idle_stop(self, seconds: float = 300.0) -> None:
        """空闲一段时间后自动释放模型占用的内存。"""
        with self.lock:
            self._cancel_idle()
            self._idle_timer = threading.Timer(seconds, self.stop)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None


BACKEND = WhisperBackend()


def _srt_time(sec: float) -> str:
    ms = int(round(float(sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_time(sec: float) -> str:
    return _srt_time(sec).replace(",", ".")


def _lrc_time(sec: float) -> str:
    m, s = divmod(float(sec), 60)
    return f"[{int(m):02d}:{s:05.2f}]"


def write_formats(data: dict, out_base: Path, formats: list[str]) -> int:
    """由 verbose_json 生成所有需要的输出格式，返回生成文件数。"""
    text = (data.get("text") or "").strip()
    segs = data.get("segments") or []
    produced = 0

    def write(ext: str, content: str) -> None:
        nonlocal produced
        out_base.with_name(f"{out_base.name}.{ext}").write_text(content, encoding="utf-8")
        produced += 1

    if not segs and text:
        segs = [{"start": 0.0, "end": 1.0, "text": text}]

    if "srt" in formats:
        blocks = []
        for i, sg in enumerate(segs, 1):
            blocks.append(f"{i}\n{_srt_time(sg['start'])} --> {_srt_time(sg['end'])}\n{sg['text'].strip()}\n")
        write("srt", "\n".join(blocks))
    if "vtt" in formats:
        blocks = ["WEBVTT\n"]
        for sg in segs:
            blocks.append(f"{_vtt_time(sg['start'])} --> {_vtt_time(sg['end'])}\n{sg['text'].strip()}\n")
        write("vtt", "\n".join(blocks))
    if "txt" in formats:
        write("txt", text + ("\n" if text else ""))
    if "lrc" in formats:
        write("lrc", "".join(f"{_lrc_time(sg['start'])}{sg['text'].strip()}\n" for sg in segs))
    if "csv" in formats:
        rows = ["start,end,text"]
        for sg in segs:
            cell = sg["text"].strip().replace('"', '""')
            rows.append(f"{sg['start']:.2f},{sg['end']:.2f},\"{cell}\"")
        write("csv", "\n".join(rows) + "\n")
    if "json" in formats:
        write("json", json.dumps({"text": text}, ensure_ascii=False))
    if "jsonfull" in formats:
        write("json", json.dumps(data, ensure_ascii=False))
    return produced


def process_one_web(src: Path, args, backend: WhisperBackend) -> tuple[float, str | None]:
    """通过常驻引擎转写单个文件。返回 (耗时秒, 错误或None)。"""
    out_dir: Path = args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rel = src.relative_to(args.base_dir).parent if args.base_dir else Path()
    except ValueError:
        rel = Path()
    target_dir = (out_dir / rel) if args.keep_structure else out_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    if not args.overwrite and all(
        (target_dir / f"{src.stem}.{f}").exists() for f in args.formats
    ):
        return 0.0, "SKIP"

    t0 = time.perf_counter()
    with transcribe.ascii_tempdir() as tmp:
        wav = tmp / "a.wav"
        transcribe.to_wav(src, wav, transcribe.FFMPEG)
        data = backend.infer(wav, args.language, args.prompt, args.translate)
        if write_formats(data, target_dir / src.stem, args.formats) == 0:
            return time.perf_counter() - t0, "未生成任何结果文件"
    return time.perf_counter() - t0, None


def run_job(payload: dict) -> None:
    try:
        inputs = [Path(x).expanduser() for x in payload.get("inputs") or []]
        if not inputs:
            raise ValueError("请先选择输入文件或文件夹")

        model_name = payload.get("model") or transcribe.MODEL
        cli, model_path, ffmpeg, model_arg = transcribe.check_env(model_name, transcribe.FFMPEG)

        args = build_args(payload)
        files = transcribe.collect_media(inputs)
        if not files:
            raise ValueError("所选路径下没有找到支持的音视频文件")

        if len(inputs) == 1 and inputs[0].is_dir():
            args.base_dir = inputs[0]

        threads = int(payload.get("threads") or 0) or transcribe.default_threads()
        workers = max(1, int(payload.get("workers") or 1))
        if "no_gpu" not in payload and not transcribe.system_info().get("gpu_recommended", True):
            args.no_gpu = True
        ctx = (cli, model_path, ffmpeg, model_arg, threads)

        with JOB_LOCK:
            JOB.state = "running"
            JOB.output_dir = args.output
            JOB.started_at = time.time()
            JOB.finished_at = None
            JOB.cancel.clear()
            JOB.files = [{"name": str(s), "status": "pending", "elapsed": 0.0, "error": None}
                         for s in files]

        JOB.log_line(f"模型 {model_path.name}（{model_path.stat().st_size/1024**3:.2f}GB）"
                     f"，语言 {args.language}，线程 {threads}，并行 {workers}")
        JOB.log_line(f"共 {len(files)} 个文件 → {args.output}")

        # 常驻推理引擎：模型只加载一次，批量时后续文件无需重载
        backend = None
        try:
            BACKEND.ensure(model_arg, threads, args.no_gpu, args.flash_attn)
            backend = BACKEND
            JOB.log_line("常驻推理引擎就绪（模型只加载一次）")
        except Exception as e:  # noqa: BLE001
            JOB.log_line(f"常驻引擎不可用，回退单文件模式：{e}")

        def mark(idx: int, status: str, elapsed: float = 0.0, error: str | None = None) -> None:
            with JOB_LOCK:
                JOB.files[idx].update(status=status, elapsed=elapsed, error=error)

        def work(i: int, src: Path) -> None:
            if JOB.cancel.is_set():
                mark(i, "fail", error="已取消")
                return
            mark(i, "running")
            t0 = time.perf_counter()
            try:
                if backend is not None:
                    try:
                        elapsed, err = process_one_web(src, args, backend)
                    except Exception:  # noqa: BLE001
                        # 常驻引擎出问题 → 自动回退到 whisper-cli 单文件模式
                        _, elapsed, err = transcribe.process_one(src, args, ctx)
                else:
                    _, elapsed, err = transcribe.process_one(src, args, ctx)
                if err == "SKIP":
                    mark(i, "skip")
                    JOB.log_line(f"[{i+1}/{len(files)}] {src.name} 已存在，跳过")
                elif err:
                    mark(i, "fail", elapsed=round(time.perf_counter() - t0, 1), error=err)
                    JOB.log_line(f"[{i+1}/{len(files)}] {src.name} 失败：{err}")
                else:
                    mark(i, "done", elapsed=round(elapsed, 1))
                    JOB.log_line(f"[{i+1}/{len(files)}] {src.name} 完成（{elapsed:.1f}s）")
            except Exception as e:  # noqa: BLE001
                mark(i, "fail", elapsed=round(time.perf_counter() - t0, 1), error=str(e))
                JOB.log_line(f"[{i+1}/{len(files)}] {src.name} 异常：{e}")

        if workers <= 1:
            for i, src in enumerate(files):
                work(i, src)
                if JOB.cancel.is_set():
                    break
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(work, i, s): i for i, s in enumerate(files)}
                for fut in as_completed(futs):
                    fut.result()
                if JOB.cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)

        with JOB_LOCK:
            JOB.state = "cancelled" if JOB.cancel.is_set() else "done"
            JOB.finished_at = time.time()
        JOB.log_line("任务结束")
        BACKEND.idle_stop()   # 5 分钟无新任务自动释放模型内存

    except Exception as e:  # noqa: BLE001
        with JOB_LOCK:
            JOB.state = "error"
            JOB.finished_at = time.time()
        JOB.log_line(f"任务失败：{e}")
        BACKEND.idle_stop()
        traceback.print_exc()


# ---------------------------------------------------------------- HTTP 处理

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---- 基础响应 ----
    def send_json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, code: int = 200,
                  ctype: str = "text/plain; charset=utf-8",
                  disposition: str | None = None) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:
        try:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            route = url.path

            if route in ("/", "/index.html"):
                self.serve_static("index.html")
            elif route == "/api/health":
                sysinfo = transcribe.system_info()
                self.send_json({
                    "ok": True,
                    "version": VERSION,
                    "whisper_dir": str(transcribe.WHISPER_DIR),
                    "models": list_models(),
                    "default_model": transcribe.MODEL,
                    "results_dir": str(results_dir()),
                    "default_results_dir": str(transcribe.RESULTS_DIR),
                    "cpu": os.cpu_count(),
                    "default_threads": transcribe.default_threads(),
                    "system": sysinfo,
                    "formats": list(transcribe.FORMAT_FLAGS),
                })
            elif route == "/api/status":
                self.send_json(JOB.snapshot())
            elif route == "/api/results":
                # 只看当前配置的结果目录，其他位置一概不算数
                rd = results_dir()
                self.send_json({
                    "results": list_results(rd),
                    "dir": str(rd),
                })
            elif route == "/api/file":
                self.preview_file(q.get("path", [""])[0], q.get("base", [""])[0],
                                  download=bool(q.get("download")))
            elif route == "/api/readme":
                self.send_readme()
            elif route.startswith("/api/"):
                self.send_json({"error": f"未知路径 {route}"}, 404)
            else:
                # 静态资源（/app.js、/style.css、/vendor/* 等，serve_static 已限制在 web/ 内）
                self.serve_static(route.lstrip("/"))
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)

    def do_POST(self) -> None:
        try:
            route = urlparse(self.path).path
            if route == "/api/start":
                payload = self.read_json()
                if JOB.state == "running":
                    self.send_json({"error": "已有任务在运行"}, 409)
                    return
                JOB.thread = threading.Thread(target=run_job, args=(payload,), daemon=True)
                JOB.thread.start()
                self.send_json({"ok": True})
            elif route == "/api/stop":
                JOB.cancel.set()
                transcribe.stop_active_procs()
                BACKEND.stop()
                self.send_json({"ok": True})
            elif route == "/api/settings":
                self.handle_settings()
            elif route == "/api/open-folder":
                rd = results_dir()
                rd.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(rd))  # 在资源管理器中打开
                else:
                    subprocess.Popen(["xdg-open", str(rd)])
                self.send_json({"ok": True, "dir": str(rd)})
            elif route == "/api/pick":
                self.handle_pick()
            else:
                self.send_json({"error": f"未知路径 {route}"}, 404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)

    def handle_settings(self) -> None:
        payload = self.read_json()
        if "results_dir" in payload:
            raw = str(payload["results_dir"] or "").strip()
            if not raw:
                self.send_json({"error": "结果目录不能为空"}, 400)
                return
            p = Path(raw).expanduser().resolve()
            if transcribe._in_system_path(p):
                self.send_json({"error": "不能把结果目录设在回收站/系统目录"}, 400)
                return
            p.mkdir(parents=True, exist_ok=True)
            cfg = load_config()
            cfg["results_dir"] = str(p)
            save_config(cfg)
            self.send_json({"ok": True, "results_dir": str(p)})
            return
        self.send_json({"ok": True})

    def handle_pick(self) -> None:
        """弹出 Windows 自带的文件/文件夹选择框，返回用户选中的绝对路径。"""
        payload = self.read_json()
        mode = payload.get("mode") or "files"
        if mode not in ("files", "dir"):
            self.send_json({"error": "mode 必须是 files 或 dir"}, 400)
            return
        try:
            r = subprocess.run(
                [sys.executable, str(BASE_DIR / "filepick.py"), mode],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600, cwd=str(BASE_DIR),
                **transcribe._spawn_kwargs())
        except (subprocess.TimeoutExpired, OSError):
            # 用户长时间未操作 / 对话框进程异常退出：按取消处理
            self.send_json({"paths": []})
            return
        paths = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode != 0 and not paths:
            err = (r.stderr or "").strip().splitlines()
            self.send_json({"error": err[-1] if err else "文件选择框调用失败"})
            return
        self.send_json({"paths": paths})

    # ---- 静态文件 & 结果预览 ----
    def send_readme(self) -> None:
        """返回使用说明原文（README.md），由前端内建 Markdown 阅读器渲染。"""
        for name in ("README.md", "README.txt"):
            f = BASE_DIR / name
            if f.is_file():
                self.send_json({
                    "ok": True,
                    "source": name,
                    "markdown": f.read_text(encoding="utf-8", errors="replace"),
                })
                return
        self.send_json({"ok": True, "source": "", "markdown": ""})

    def serve_static(self, name: str) -> None:
        f = (WEB_DIR / name).resolve()
        if not str(f).startswith(str(WEB_DIR.resolve())) or not f.is_file():
            self.send_json({"error": "not found"}, 404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, mimetypes.guess_type(f.name)[0]
                                                  or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def preview_file(self, path: str, base: str, download: bool = False) -> None:
        f = Path(path).resolve()
        b = results_dir().resolve()
        if not (str(f).startswith(str(b) + os.sep) or f == b):
            self.send_json({"error": "只能预览转写结果目录内的文件"}, 403)
            return
        if transcribe._in_system_path(f):
            self.send_json({"error": "拒绝访问回收站/系统目录"}, 403)
            return
        if not f.is_file():
            self.send_json({"error": f"文件不存在：{f}"}, 404)
            return

        if download:
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{f.name.encode('utf-8').decode('latin-1')}")
            self.end_headers()
            self.wfile.write(data)
            return

        # 文本预览：超过 200KB 只返回开头，避免大文本把浏览器渲染卡死
        size = f.stat().st_size
        cap = 200 * 1024
        try:
            if size > cap:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(cap)
                self.send_text(
                    head +
                    f"\n\n…（文件共 {size / 1024:.0f}KB，仅显示前 200KB，完整内容请点「下载」）"
                )
            else:
                self.send_text(f.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            self.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------- 启动

def _port_in_use(port: int) -> bool:
    """探测端口是否已被别的实例占用（Windows 允许双绑定，必须主动检测）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    ap = argparse.ArgumentParser(description="语音转文字本地 Web GUI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    METRICS.start()   # 系统监控（CPU/内存/温度）

    port = args.port
    if _port_in_use(port):
        # 已有实例在跑：换随机端口，避免两个实例抢同一端口、浏览器连到旧的
        print(f"[提示] 端口 {port} 已被占用（可能已有实例在运行），本次使用随机端口")
        port = 0
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:  # 端口被占用则随机分配
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    url = f"http://127.0.0.1:{port}"
    print("=" * 52)
    print("  语音转文字 GUI 已启动")
    print(f"  地址：{url}")
    print("  （本机专用，不会暴露给局域网；关闭本窗口即退出）")
    print("=" * 52)
    sys.stdout.flush()

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务")
        transcribe.stop_active_procs()


if __name__ == "__main__":
    main()
