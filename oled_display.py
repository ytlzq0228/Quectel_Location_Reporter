# oled_display.py - SSD1306 128x64 I2C OLED 显示驱动
#
# 对外接口：init_oled, clear, show_boot_message, update_position,
#          reset_display_compact, update_display_compact
# 字库：小号 12px、大号 32px（font_to_py 生成），不内嵌字模。

import utime
from machine import I2C

# -----------------------------------------------------------------------------
# 硬件与 SSD1306 常量
# -----------------------------------------------------------------------------
I2C_PORT = I2C.I2C0
I2C_MODE = I2C.STANDARD_MODE
SSD1306_ADDR = 0x3C
WIDTH = 128
HEIGHT = 64
PAGES = 8
I2C_CHUNK = 32

_CMD = bytearray([0x00])
_DATA = bytearray([0x40])


# -----------------------------------------------------------------------------
# 字库加载（仅引用预生成字库，不内嵌）
# -----------------------------------------------------------------------------
def _wrap_font(g):
    """将 exec 得到的字库 dict 封装成与模块一致接口（.get_ch(c) / .height() / .max_width()）。"""
    if g is None:
        return None
    if hasattr(g, "get_ch") and callable(getattr(g, "get_ch", None)):
        return g
    if isinstance(g, dict) and "get_ch" in g and "height" in g and "max_width" in g:
        class _F:
            get_ch = lambda self, c: g["get_ch"](c)
            height = lambda self: g["height"]()
            max_width = lambda self: g["max_width"]()
        return _F()
    return g


def _load_font_py(paths, mod_name):
    """先尝试 import 模块，再尝试 open+exec 路径；返回带 get_ch/height/max_width 的对象。"""
    raw = None
    try:
        mod = __import__(mod_name)
        if hasattr(mod, "get_ch") and hasattr(mod, "height"):
            raw = mod
    except Exception:
        pass
    if raw is None:
        try:
            pkg = __import__("Fonts", fromlist=[mod_name])
            raw = getattr(pkg, mod_name, None)
        except Exception:
            pass
    if raw is None:
        for path in paths:
            try:
                with open(path) as f:
                    g = {}
                    exec(f.read(), g)
                if "get_ch" in g and "height" in g and "max_width" in g:
                    raw = g
                    break
            except Exception:
                continue
    return _wrap_font(raw)



font_12 = _load_font_py(("Fonts/PixelOperator_12.py", "/usr/Fonts/PixelOperator_12.py", "PixelOperator_12.py"), "PixelOperator_12")
if font_12 is None:
    font_12 = _load_font_py(("Fonts/font_12.py", "/usr/Fonts/font_12.py", "font_12.py"), "font_12")
font_32 = _load_font_py(("Fonts/PixelOperator_32.py", "/usr/Fonts/PixelOperator_32.py", "PixelOperator_32.py"), "PixelOperator_32")
if font_32 is None:
    font_32 = _load_font_py(("Fonts/font_32.py", "/usr/Fonts/font_32.py", "font_32.py"), "font_32")

if font_12 is None:
    raise ImportError("oled_display requires 12px font (e.g. Fonts/PixelOperator_12.py).")
if font_32 is None:
    raise ImportError("oled_display requires 32px font (e.g. Fonts/PixelOperator_32.py).")

# 小号 12px，大号 32px
font_small = font_12
font_large = font_32

# 派生尺寸（用于布局与清区域）
SMALL_H_PAGES = (font_small.height() + 7) // 8
SMALL_MAX_COL_PER_CHAR = font_small.max_width() + 1
LARGE_H_PAGES = (font_large.height() + 7) // 8
LARGE_MAX_COL_PER_CHAR = font_large.max_width() + 1


# -----------------------------------------------------------------------------
# 字模转 SSD1306 格式（font_to_py 为行主序 MONO_HMSB，SSD1306 为列/页）
# -----------------------------------------------------------------------------
def _glyph_to_ssd1306(glyph_bytes, width, height):
    """将 get_ch 返回的 (bytes, height, width) 转为 SSD1306 列×页缓冲。每列 1 空列 + width 列，每列 h_pages 字节。"""
    if hasattr(glyph_bytes, "__getitem__"):
        g = glyph_bytes
    else:
        g = bytes(glyph_bytes)
    bpr = (width + 7) // 8
    h_pages = (height + 7) // 8
    cols = width + 1
    buf = bytearray(cols * h_pages)
    for c in range(width):
        for p in range(h_pages):
            out_byte = 0
            for row in range(8):
                y = p * 8 + row
                if y >= height:
                    break
                byte_idx = y * bpr + c // 8
                if byte_idx < len(g):
                    bit_idx = 7 - (c % 8)
                    if (g[byte_idx] >> bit_idx) & 1:
                        # 页内 bit0=上、bit7=下，与 SSD1306 COM 顺序一致，避免上下颠倒
                        out_byte |= 1 << row
                buf[p * cols + 1 + c] = out_byte
    return buf


def _draw_char(i2c, page, col, ch, font):
    """在 (page, col) 画一个字符，返回前进列数（字宽+1）。"""
    try:
        glyph, h, w = font.get_ch(ch)
    except Exception:
        try:
            glyph, h, w = font.get_ch("?")
        except Exception:
            return font.max_width() + 1
    buf = _glyph_to_ssd1306(glyph, w, h)
    cols = w + 1
    h_pages = (h + 7) // 8
    _set_region(i2c, col, col + cols - 1, page, page + h_pages - 1)
    _write_data(i2c, buf)
    return w + 1


def _draw_string(i2c, page, col, s, font):
    """从 (page, col) 起画字符串 s，使用给定 font。"""
    for c in s:
        adv = _draw_char(i2c, page, col, c, font)
        col += adv
        if col >= WIDTH:
            break


def _draw_number(i2c, page, col, s, font):
    """画数字串（如 '000'），用 font。返回结束列。"""
    for c in s:
        if col >= WIDTH:
            break
        adv = _draw_char(i2c, page, col, c, font)
        col += adv
    return col


def _measure_number_cols(s, font):
    """测量数字串在当前字体下大约占用的列数（包含 1 列间隔）。"""
    total = 0
    for c in s:
        try:
            _, _, w = font.get_ch(c)
        except Exception:
            try:
                _, _, w = font.get_ch("0")
            except Exception:
                w = font.max_width()
        total += w + 1
    return total


def _draw_number_right(i2c, page, right_col, s, font):
    """使数字串的最右侧对齐到 right_col。"""
    width_cols = _measure_number_cols(s, font)
    start_col = right_col - width_cols + 1
    if start_col < 0:
        start_col = 0
    _draw_number(i2c, page, start_col, s, font)


# -----------------------------------------------------------------------------
# SSD1306 底层
# -----------------------------------------------------------------------------
def _cmd(i2c, *bytes_list):
    if not bytes_list:
        return
    b = bytearray(bytes_list)
    i2c.write(SSD1306_ADDR, _CMD, 1, b, len(b))


def _write_data(i2c, data):
    if not data:
        return
    n = len(data)
    off = 0
    while off < n:
        end = min(off + I2C_CHUNK, n)
        i2c.write(SSD1306_ADDR, _DATA, 1, data[off:end], end - off)
        off = end
        if off < n:
            utime.sleep_us(100)


def _set_region(i2c, col_start, col_end, page_start, page_end):
    _cmd(i2c, 0x21, col_start, col_end)
    _cmd(i2c, 0x22, page_start, page_end)


def _fill_rect(i2c, col_start, col_end, page_start, page_end, fill=0x00):
    _set_region(i2c, col_start, col_end, page_start, page_end)
    w = col_end - col_start + 1
    h = page_end - page_start + 1
    _write_data(i2c, bytearray([fill] * (w * h)))


def _ssd1306_init(i2c):
    _cmd(i2c, 0xAE)
    _cmd(i2c, 0xD5, 0x80)
    _cmd(i2c, 0xA8, 0x3F)
    _cmd(i2c, 0xD3, 0)
    _cmd(i2c, 0x40)
    _cmd(i2c, 0x8D, 0x14)
    _cmd(i2c, 0x20, 0x00)
    _cmd(i2c, 0xA1)
    _cmd(i2c, 0xC8)
    _cmd(i2c, 0xDA, 0x12)
    _cmd(i2c, 0x81, 0xCF)
    _cmd(i2c, 0xD9, 0xF1)
    _cmd(i2c, 0xDB, 0x40)
    _cmd(i2c, 0xA4)
    _cmd(i2c, 0xA6)
    _cmd(i2c, 0x2E)
    _cmd(i2c, 0xAF)


# -----------------------------------------------------------------------------
# 电池图标（缩小版：12 列×12 行，正极凸点在右侧）
# -----------------------------------------------------------------------------
BAT_COL_START = 116
BAT_COL_END = 127
BAT_PAGE_START = 0
BAT_PAGE_END = 1
BAT_SEGMENTS = 8


def _draw_battery(i2c, seg_count):
    """在右侧画小电池：0..BAT_SEGMENTS 段。本体在左，正极凸起在图标右侧。"""
    w = BAT_COL_END - BAT_COL_START + 1   # 12
    np = BAT_PAGE_END - BAT_PAGE_START + 1
    buf = bytearray(w * np)

    def set_pixel(c, y):
        if 0 <= c < w and 0 <= y < 16:
            p = y // 8
            bit = y % 8
            buf[p * w + c] |= 1 << bit

    # 本体外框：左 x=0，右 x=8；上 y=2，下 y=10
    for cx in range(9):
        set_pixel(cx, 2)
        set_pixel(cx, 10)
    for ry in range(2, 11):
        set_pixel(0, ry)
        set_pixel(8, ry)
    # 正极凸起：右侧 2 列 (x=10,11)，竖直居中 y=4..8
    for cx in range(10, 12):
        for ry in range(4, 9):
            set_pixel(cx, ry)
    # 电量填充：x=1..8，y=3..9，共 8 段
    seg = max(0, min(seg_count, BAT_SEGMENTS))
    for i in range(seg):
        for ry in range(3, 10):
            set_pixel(1 + i, ry)

    _set_region(i2c, BAT_COL_START, BAT_COL_END, BAT_PAGE_START, BAT_PAGE_END)
    _write_data(i2c, buf)


# -----------------------------------------------------------------------------
# 布局常量：主界面（经纬度 + 速度 + 类型 + 更新时间 + 电量）
# 小号 12px = 2 页/行，大号 32px = 4 页；共 8 页，行间不重叠。
# -----------------------------------------------------------------------------
# 行 0: 标题(左) + Speed 标签(右) + 电量
PAGE_TITLE = 0           # 占页 0-1
# 行 1: 纬度(左) + 速度大号(右，占 4 页)
PAGE_LAT = 2             # 占页 2-3
# 行 2: 经度(左) + 速度大号(右)
PAGE_LON = 4             # 占页 4-5
# 行 3: 类型 + 更新时间 合并一行(左)
PAGE_TYPE_UPD = 6        # 占页 6-7
# 速度大号：右侧，占页 2-5
PAGE_SPD_START = 2
PAGE_SPD_END = 5

COL_TITLE = 2
COL_LAT = 1
COL_LON = 1
COL_TYPE_UPD = 1
COL_LEFT_MAX = 54        # 左侧内容止于 54，与速度区不重叠

LAT_MAX_CH = 11
LON_MAX_CH = 11
TYPE_MAX_CH = 6
UPD_MAX_CH = 10
TYPE_UPD_MAX_CH = 18     # 合并行总字符约 18

SPD_COL_RIGHT = WIDTH - 1    # 速度数值最右对齐到屏幕右边界


# -----------------------------------------------------------------------------
# 布局常量：紧凑界面（无经纬度，含 APRS/Traccar/精度）
# 页 0-1: 标题+电量；页 2-3: 类型(左)+速度(右)；页 4-5: APRS/精度(左)；页 6-7: Traccar(左)
# -----------------------------------------------------------------------------
PAGE_C_TITLE = 0         # 占页 0-1
PAGE_C_SPD_START = 2
PAGE_C_SPD_END = 5       # 速度 32px 占页 2-5
PAGE_C_TYPE = 2         # 占页 2-3
PAGE_C_APRS = 4         # 占页 4-5
PAGE_C_TRACCAR = 6      # 占页 6-7
COL_C_TITLE = 2
COL_C_LEFT = 1
COL_C_LEFT_MAX = 54
C_TITLE_LEN = 10
C_TYPE_LEN = 11
C_LINE_LEN = 18

SPD_COL_RIGHT_C = WIDTH - 1   # 紧凑布局下速度右对齐到屏幕右边界


# -----------------------------------------------------------------------------
# 启动多行显示
# -----------------------------------------------------------------------------
BOOT_MAX_LINES = min(6, PAGES // SMALL_H_PAGES) if PAGES >= SMALL_H_PAGES else 1
BOOT_CHARS_PER_LINE = 21
_state_boot = []


def _format_ago(sec):
    if sec is None or sec < 0:
        return "--"
    s = int(sec)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


# -----------------------------------------------------------------------------
# 对外接口
# -----------------------------------------------------------------------------
def init_oled():
    """初始化 I2C 与 SSD1306，返回 i2c 对象；失败返回 None。"""
    try:
        i2c = I2C(I2C_PORT, I2C_MODE)
        utime.sleep_ms(50)
        _ssd1306_init(i2c)
        utime.sleep_ms(50)
        return i2c
    except Exception as e:
        print("oled_display init_oled error:", e)
        return None


def clear(i2c, fill=0x00):
    """整屏填充；程序退出时调用可黑屏。"""
    global _state_boot
    if i2c is None:
        return
    try:
        _set_region(i2c, 0, WIDTH - 1, 0, PAGES - 1)
        _write_data(i2c, bytearray([fill] * (WIDTH * PAGES)))
        _state_boot = []
    except Exception as e:
        print("oled_display clear error:", e)


def show_boot_message(i2c, msg="Booting..."):
    """多行追加显示，每次调用追加一行（最多 BOOT_MAX_LINES 行），超出则上滚。"""
    global _state_boot
    if i2c is None:
        return
    try:
        line = str(msg)[:BOOT_CHARS_PER_LINE]
        n = len(_state_boot)
        if n < BOOT_MAX_LINES:
            if n == 0:
                _fill_rect(i2c, 0, WIDTH - 1, 0, BOOT_MAX_LINES * SMALL_H_PAGES - 1, 0x00)
            _state_boot.append(line)
            row = len(_state_boot) - 1
            page_start = row * SMALL_H_PAGES
            if n > 0:
                _fill_rect(i2c, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, page_start, 0, line, font_small)
        else:
            _state_boot = _state_boot[1:] + [line]
            for row in range(BOOT_MAX_LINES):
                page_start = row * SMALL_H_PAGES
                _fill_rect(i2c, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
                _draw_string(i2c, page_start, 0, _state_boot[row], font_small)
    except Exception as e:
        print("oled_display show_boot_message error:", e)


# 主界面状态（增量更新）
_state = {
    "init_done": False,
    "prev_lat": None,
    "prev_lon": None,
    "prev_speed": None,
    "prev_type_upd": None,
    "prev_bat": None,
    "oled_error_logged": False,
}


def _draw_static_labels(i2c):
    _draw_string(i2c, PAGE_TITLE, COL_TITLE, "GNSS INFO", font_small)


def update_position(i2c, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=None):
    """
    增量更新主界面：经纬度、速度、类型、更新时间、电量。
    i2c 为 None 或写失败时静默返回。
    """
    try:
        if i2c is None:
            return
        s = _state
        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))

        if not s["init_done"]:
            _draw_static_labels(i2c)
            s["init_done"] = True
            s["prev_lat"] = ""
            s["prev_lon"] = ""
            s["prev_speed"] = ""
            s["prev_type_upd"] = ""
            s["prev_bat"] = -1

        lat_disp = (lat_disp or "---")[:LAT_MAX_CH]
        lon_disp = (lon_disp or "---")[:LON_MAX_CH]
        gnss_type = (gnss_type or "---")[:TYPE_MAX_CH]
        upd_str = (update_time or "") + (str(time_dif) if time_dif is not None else "")
        type_upd_line = ("Type:" + gnss_type + " Upd:" + upd_str)[:TYPE_UPD_MAX_CH]

        if lat_disp != s["prev_lat"]:
            _fill_rect(i2c, COL_LAT, COL_LEFT_MAX, PAGE_LAT, PAGE_LAT + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_LAT, COL_LAT, lat_disp, font_small)
            s["prev_lat"] = lat_disp

        if lon_disp != s["prev_lon"]:
            _fill_rect(i2c, COL_LON, COL_LEFT_MAX, PAGE_LON, PAGE_LON + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_LON, COL_LON, lon_disp, font_small)
            s["prev_lon"] = lon_disp

        if speed_str != s["prev_speed"]:
            # 只清空速度数字实际占用的列，再右对齐绘制
            w = _measure_number_cols(speed_str, font_large)
            spd_start = max(COL_LEFT_MAX + 1, SPD_COL_RIGHT - w + 1)
            _fill_rect(i2c, spd_start, SPD_COL_RIGHT, PAGE_SPD_START, PAGE_SPD_END - 1, 0x00)
            _draw_number_right(i2c, PAGE_SPD_START, SPD_COL_RIGHT, speed_str, font_large)
            s["prev_speed"] = speed_str

        if type_upd_line != (s.get("prev_type_upd") or ""):
            _fill_rect(i2c, COL_TYPE_UPD, COL_LEFT_MAX, PAGE_TYPE_UPD, PAGE_TYPE_UPD + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_TYPE_UPD, COL_TYPE_UPD, type_upd_line, font_small)
            s["prev_type_upd"] = type_upd_line

        if bat_pct is not None and bat_seg != s["prev_bat"]:
            _draw_battery(i2c, bat_seg)
            s["prev_bat"] = bat_seg

        s["oled_error_logged"] = False
    except (OSError, Exception) as e:
        try:
            if not s.get("oled_error_logged"):
                print("oled_display: I2C error (screen not connected?):", e)
                s["oled_error_logged"] = True
        except NameError:
            print("oled_display update_position:", e)
    except Exception as e:
        print("oled_display update_position error:", e)


# 紧凑布局状态
_state_compact = {
    "init_done": False,
    "prev_title": None,
    "prev_bat": None,
    "prev_speed": None,
    "prev_type": None,
    "prev_aprs_ago": None,
    "prev_traccar_ago": None,
    "prev_accuracy": None,
    "oled_error_logged": False,
}


def reset_display_compact():
    """重置紧凑布局缓存；清屏后再次显示前调用。"""
    try:
        sc = _state_compact
        sc["init_done"] = False
        sc["prev_title"] = None
        sc["prev_bat"] = None
        sc["prev_speed"] = None
        sc["prev_type"] = None
        sc["prev_aprs_ago"] = None
        sc["prev_traccar_ago"] = None
        sc["prev_accuracy"] = None
    except Exception as e:
        print("oled_display reset_display_compact error:", e)


def _draw_compact_static(i2c, title):
    _draw_string(i2c, PAGE_C_TITLE, COL_C_TITLE, (title or "Quec GNSS")[:C_TITLE_LEN], font_small)


def update_display_compact(
    i2c,
    title="Quec GNSS",
    bat_pct=None,
    speed_kmh=None,
    gnss_type=None,
    aprs_ago_sec=None,
    traccar_ago_sec=None,
    accuracy_m=None,
):
    """
    紧凑布局增量更新：标题、电量、速度、定位方式、APRS/Traccar 距上次上报、精度。
    aprs_ago_sec / traccar_ago_sec: 秒数，None 显示 --；accuracy_m: 米数，None 显示 --。
    """
    try:
        if i2c is None:
            return
        sc = _state_compact
        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))
        type_str = (gnss_type or "---")[:8]
        aprs_str = _format_ago(aprs_ago_sec)
        traccar_str = _format_ago(traccar_ago_sec)
        if accuracy_m is not None:
            try:
                acc_str = "%.1fm" % float(accuracy_m)
            except (TypeError, ValueError):
                acc_str = "--"
        else:
            acc_str = "--"
        line_aprs = ("APRS:%s Acc:%s" % (aprs_str, acc_str))[:C_LINE_LEN]
        line_trcr = ("Trcr:%s" % traccar_str)[:C_LINE_LEN]

        if not sc["init_done"]:
            _draw_compact_static(i2c, title)
            sc["init_done"] = True
            sc["prev_title"] = ""
            sc["prev_bat"] = -1
            sc["prev_speed"] = ""
            sc["prev_type"] = ""
            sc["prev_aprs_ago"] = ""
            sc["prev_traccar_ago"] = ""
            sc["prev_accuracy"] = ""

        title_disp = (title or "Quec GNSS")[:C_TITLE_LEN]
        if title_disp != sc["prev_title"]:
            _fill_rect(i2c, COL_C_TITLE, COL_C_LEFT_MAX, PAGE_C_TITLE, PAGE_C_TITLE + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_C_TITLE, COL_C_TITLE, title_disp, font_small)
            sc["prev_title"] = title_disp

        if bat_pct is not None and bat_seg != sc["prev_bat"]:
            _draw_battery(i2c, bat_seg)
            sc["prev_bat"] = bat_seg

        if speed_str != sc["prev_speed"]:
            # 只清空速度数字实际占用的列，再右对齐绘制
            w = _measure_number_cols(speed_str, font_large)
            spd_start = max(COL_C_LEFT_MAX + 1, SPD_COL_RIGHT_C - w + 1)
            _fill_rect(i2c, spd_start, SPD_COL_RIGHT_C, PAGE_C_SPD_START, PAGE_C_SPD_END - 1, 0x00)
            _draw_number_right(i2c, PAGE_C_SPD_START, SPD_COL_RIGHT_C, speed_str, font_large)
            sc["prev_speed"] = speed_str

        type_disp = ("Type:" + type_str)[:C_TYPE_LEN]
        if type_disp != sc["prev_type"]:
            _fill_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TYPE, PAGE_C_TYPE + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_C_TYPE, COL_C_LEFT, type_disp, font_small)
            sc["prev_type"] = type_disp

        if line_aprs != sc["prev_aprs_ago"]:
            _fill_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_APRS, PAGE_C_APRS + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_C_APRS, COL_C_LEFT, line_aprs, font_small)
            sc["prev_aprs_ago"] = line_aprs

        if line_trcr != sc["prev_traccar_ago"]:
            _fill_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TRACCAR, PAGE_C_TRACCAR + SMALL_H_PAGES - 1, 0x00)
            _draw_string(i2c, PAGE_C_TRACCAR, COL_C_LEFT, line_trcr, font_small)
            sc["prev_traccar_ago"] = line_trcr

        sc["oled_error_logged"] = False
    except (OSError, Exception) as e:
        try:
            if not sc.get("oled_error_logged"):
                print("oled_display (compact): I2C error:", e)
                sc["oled_error_logged"] = True
        except NameError:
            print("oled_display (compact):", e)
    except Exception as e:
        print("oled_display update_display_compact error:", e)
