# -*- coding: utf-8 -*-
"""
FOTA 程序：从 GitHub 仓库下载 Quectel_Location_Reporter 非测试文件到 /usr。
基于 QuecPython app_fota 模块：https://developer.quectel.com/doc/quecpython/API_reference/zh/syslib/app_fota.html
运行前请确保模组已联网；执行完成后将设置升级标志并重启，重启后生效。
"""

import app_fota
from misc import Power

# GitHub 仓库 raw 地址（main 分支）
REPO_RAW = "https://raw.githubusercontent.com/ytlzq0228/Quectel_Location_Reporter/main"

# 需要下载到 /usr 的非测试文件（不含 test_*.py、tools、docs、模拟器等）
# url 用 REPO_RAW 拼接；file_name 为设备上的绝对路径
DOWNLOAD_LIST = [
    {"url": REPO_RAW + "/config.py", "file_name": "/usr/config.py"},
    {"url": REPO_RAW + "/main.py", "file_name": "/usr/main.py"},
    {"url": REPO_RAW + "/GNSS_Reporter.py", "file_name": "/usr/GNSS_Reporter.py"},
    {"url": REPO_RAW + "/traccar_report.py", "file_name": "/usr/traccar_report.py"},
    {"url": REPO_RAW + "/aprs_report.py", "file_name": "/usr/aprs_report.py"},
    {"url": REPO_RAW + "/battery.py", "file_name": "/usr/battery.py"},
    {"url": REPO_RAW + "/cell_info.py", "file_name": "/usr/cell_info.py"},
    {"url": REPO_RAW + "/oled_display.py", "file_name": "/usr/oled_display.py"},
    {"url": REPO_RAW + "/config.cfg.example", "file_name": "/usr/config.cfg.example"},
    {"url": REPO_RAW + "/Fonts/PixelOperator_12.py", "file_name": "/usr/Fonts/PixelOperator_12.py"},
    {"url": REPO_RAW + "/Fonts/PixelOperator_32.py", "file_name": "/usr/Fonts/PixelOperator_32.py"},
]


def ensure_fonts_dir():
    """确保 /usr/Fonts 目录存在（app_fota 下载到文件，不会自动建目录）。"""
    try:
        import uos
        try:
            uos.mkdir("/usr/Fonts")
        except OSError:
            pass  # 已存在则忽略
    except Exception:
        pass


def run_fota():
    ensure_fonts_dir()
    fota = app_fota.new()
    failed = fota.bulk_download(DOWNLOAD_LIST)
    if failed:
        print("FOTA 部分失败:", failed)
        return
    fota.set_update_flag()
    Power.powerRestart()


if __name__ == "__main__":
    run_fota()
