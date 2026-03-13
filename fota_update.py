# -*- coding: utf-8 -*-
"""
FOTA 程序：从 GitHub 仓库下载 Quectel_Location_Reporter 非测试文件到 /usr。
基于 QuecPython app_fota 模块：https://developer.quectel.com/doc/quecpython/API_reference/zh/syslib/app_fota.html
运行前请确保模组已联网；执行完成后将设置升级标志并重启，重启后生效。
"""

import app_fota
import utime
from misc import Power


# GitHub 仓库 raw 地址（main 分支）
#REPO_RAW = "https://raw.githubusercontent.com/ytlzq0228/Quectel_Location_Reporter/main"
REPO_RAW = "https://traccar.ctsdn.com:4343/gitproxy/ytlzq0228/Quectel_Location_Reporter/main"
# 需要下载到 /usr 的非测试文件（不含 test_*.py、tools、docs、模拟器等）
# url 用 REPO_RAW 拼接；file_name 为设备上的绝对路径
DOWNLOAD_LIST = [
    {"url": REPO_RAW + "/fota_update.py", "file_name": "/usr/fota_update.py"},
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


def run_fota_with_progress(oled_status_cb=None, log_info_cb=None):
    """
    执行 FOTA，逐个文件下载，实时在 OLED 和 log 显示进度。
    oled_status_cb(msg): 显示短状态，如 "FOTA 3/12"
    log_info_cb(msg): 记录过程日志
    结束（正常或异常）时一定会 Power.powerRestart()，由本模块保证，主进程不负责重启。
    返回: failed_list，空表示全部成功（调用方通常不会收到返回，因会先重启）。
    """
    try:
        ensure_fonts_dir()
        n = len(DOWNLOAD_LIST)
        if log_info_cb:
            log_info_cb("FOTA start, %d files" % n)
        if oled_status_cb:
            oled_status_cb("FOTA 0/%d" % n)
        fota = app_fota.new()
        failed = []
        for i, item in enumerate(DOWNLOAD_LIST):
            url, path = item["url"], item["file_name"]
            if oled_status_cb:
                oled_status_cb("FOTA %d/%d" % (i + 1, n))
            if log_info_cb:
                log_info_cb("FOTA %d/%d %s" % (i + 1, n, path))
            try:
                ret = fota.download(url, path)
                if ret != 0:
                    failed.append(item)
                    if log_info_cb:
                        log_info_cb("FOTA fail: %s" % path)
            except Exception as e:
                failed.append(item)
                if log_info_cb:
                    log_info_cb("FOTA fail: %s %s" % (path, e))
        if not failed:
            if log_info_cb:
                log_info_cb("FOTA all ok, set_update_flag")
            if oled_status_cb:
                oled_status_cb("FOTA all ok")
            fota.set_update_flag()
        else:
            if log_info_cb:
                log_info_cb("FOTA partial fail: %s" % len(failed))
            if oled_status_cb:
                oled_status_cb("FOTA fail %d" % len(failed))
        if log_info_cb:
            log_info_cb("FOTA done, Device restart")
        if oled_status_cb:
            oled_status_cb("Device restart")
        return failed
    finally:
        # 留 2 秒让 log/OLED 显示完再重启，避免“没显示完就重启”
        utime.sleep(2)
        Power.powerRestart()


def run_fota():
    """独立运行：带简单进度输出，结束后由 run_fota_with_progress 的 finally 统一重启。"""
    failed = run_fota_with_progress(oled_status_cb=print, log_info_cb=print)
    if failed:
        print("FOTA 部分失败:", failed)


if __name__ == "__main__":
    run_fota()
