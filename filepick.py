#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弹出 Windows 自带的文件/文件夹选择对话框（系统原生资源管理器界面），
把选中的绝对路径逐行打印到 stdout。

用法：
    python filepick.py files       选择一个或多个文件（可多选）
    python filepick.py dir         选择一个文件夹
    python filepick.py --selftest  自检（不弹窗）

由 server.py 作为子进程调用；单独运行也可以。
用户取消时输出为空，退出码 0。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import POINTER, Structure, byref, c_int, c_void_p, cast, wintypes

# 中文路径必须按 UTF-8 输出（管道默认是 GBK，服务端会解码错乱）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


# ---------------- 对话框置顶（否则会被浏览器窗口挡住） ----------------

def _start_foreground_watchdog() -> None:
    """后台线程：找到本进程弹出的对话框后，把它钉在最前面并激活。"""
    import os
    import time

    user32 = ctypes.windll.user32
    pid = os.getpid()
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040

    def raise_our_windows() -> None:
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _lparam):
            pid_found = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_found))
            if pid_found.value == pid and user32.IsWindowVisible(hwnd):
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            return True

        user32.EnumWindows(enum_cb, 0)

    def loop() -> None:
        for _ in range(240):      # 最多 12 秒，期间持续置顶
            try:
                raise_our_windows()
            except Exception:  # noqa: BLE001
                return
            time.sleep(0.25)

    threading.Thread(target=loop, daemon=True).start()

# ---------------- 通用文件打开对话框（资源管理器界面，可多选） ----------------

OFN_EXPLORER = 0x00080000
OFN_ALLOWMULTISELECT = 0x00000200
OFN_FILEMUSTEXIST = 0x00001000
OFN_PATHMUSTEXIST = 0x00000800
OFN_HIDEREADONLY = 0x00000004

MEDIA_FILTER = (
    "音视频文件\0*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.opus;*.wma;*.amr;*.aiff;"
    "*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.webm;*.ts;*.m4v;*.mpg;*.mpeg\0"
    "所有文件\0*.*\0\0"
)


class OPENFILENAMEW(Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def pick_files(title: str = "选择音视频文件（可多选）") -> list[str]:
    """系统文件选择框；返回绝对路径列表（用户取消则为空）。"""
    buf = ctypes.create_unicode_buffer(32768)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = MEDIA_FILTER
    ofn.nFilterIndex = 1
    ofn.lpstrFile = cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 32768
    ofn.lpstrTitle = title
    ofn.Flags = (OFN_EXPLORER | OFN_ALLOWMULTISELECT |
                 OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY)

    if not ctypes.windll.comdlg32.GetOpenFileNameW(byref(ofn)):
        return []
    # 用 wstring_at 读完整缓冲区（含嵌入的 NUL，多选时是 目录\0文件1\0文件2\0\0）
    raw = ctypes.wstring_at(ctypes.addressof(buf), 32768)
    items = [p for p in raw.split("\0") if p]
    if not items:
        return []
    if len(items) == 1:          # 单选：直接是完整路径
        return [items[0]]
    directory, names = items[0], items[1:]   # 多选：目录 + 若干文件名
    return [f"{directory}\\{n}" for n in names]


# ---------------- 文件夹选择对话框 ----------------

BIF_RETURNONLYFSDIRS = 0x0001
BIF_NEWDIALOGSTYLE = 0x0040


class BROWSEINFOW(Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", c_int),
    ]


def pick_dir(title: str = "选择文件夹") -> str | None:
    """系统文件夹选择框；返回绝对路径（用户取消则为 None）。"""
    ctypes.windll.ole32.CoInitialize(None)
    namebuf = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.pszDisplayName = cast(namebuf, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE

    pidl = ctypes.windll.shell32.SHBrowseForFolderW(byref(bi))
    if not pidl:
        return None
    path = ctypes.create_unicode_buffer(1024)
    ok = ctypes.windll.shell32.SHGetPathFromIDListW(pidl, path)
    ctypes.windll.ole32.CoTaskMemFree(pidl)
    return path.value if ok else None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "files"
    if mode == "--selftest":
        print(f"OPENFILENAMEW size = {ctypes.sizeof(OPENFILENAMEW)} (x64 期望 152)")
        print(f"BROWSEINFOW size   = {ctypes.sizeof(BROWSEINFOW)} (x64 期望 64)")
        assert ctypes.sizeof(OPENFILENAMEW) == 152
        assert ctypes.sizeof(BROWSEINFOW) == 64
        print("selftest OK")
        return 0
    if mode not in ("dir", "files"):
        print("用法: python filepick.py files|dir|--selftest", file=sys.stderr)
        return 2

    _start_foreground_watchdog()   # 对话框钉在最前面

    if mode == "dir":
        p = pick_dir()
        if p:
            print(p)
        return 0
    for p in pick_files():
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
