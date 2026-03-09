# oled_display.py - SSD1306 128x64 I2C OLED 运行状态显示（增量更新，提高刷新速度）
#
# 参考 ssd1306_i2c_test.py 的 I2C/SSD1306 操作，布局参考 OLED_Position（经纬度、速度、类型、更新时间、电量）。
# 仅重绘发生变化的区域，减少 I2C 写入量以提升刷新速度。

import utime
from machine import I2C

# ------------------------- I2C 与 SSD1306 配置 -------------------------
I2C_PORT = I2C.I2C0
I2C_MODE = I2C.STANDARD_MODE
SSD1306_ADDR = 0x3C
WIDTH = 128
HEIGHT = 64
PAGES = 8
I2C_DATA_CHUNK = 32

CMD_STREAM = bytearray([0x00])
DATA_STREAM = bytearray([0x40])

# 5x7 点阵字模（MSB=上），补全显示所需字符
FONT5X7 = {
    0x20: (0x00, 0x00, 0x00, 0x00, 0x00),
    0x2D: (0x08, 0x08, 0x08, 0x08, 0x08),   # -
    0x2E: (0x00, 0x00, 0x00, 0x03, 0x03),   # .
    0x2F: (0x40, 0x20, 0x10, 0x08, 0x04),   # /
    0x30: (0x3E, 0x51, 0x49, 0x45, 0x3E),
    0x31: (0x00, 0x42, 0x7F, 0x40, 0x00),
    0x32: (0x62, 0x51, 0x49, 0x49, 0x46),
    0x33: (0x22, 0x49, 0x49, 0x49, 0x36),
    0x34: (0x18, 0x14, 0x12, 0x7F, 0x10),
    0x35: (0x27, 0x45, 0x45, 0x45, 0x39),
    0x36: (0x3C, 0x4A, 0x49, 0x49, 0x31),
    0x37: (0x41, 0x21, 0x11, 0x09, 0x07),
    0x38: (0x36, 0x49, 0x49, 0x49, 0x36),
    0x39: (0x46, 0x49, 0x49, 0x29, 0x1E),
    0x3A: (0x00, 0x36, 0x36, 0x00, 0x00),   # :
    0x41: (0x7C, 0x12, 0x11, 0x12, 0x7C),
    0x42: (0x7F, 0x49, 0x49, 0x49, 0x36),
    0x43: (0x3E, 0x41, 0x41, 0x41, 0x22),
    0x44: (0x7F, 0x41, 0x41, 0x41, 0x3E),
    0x45: (0x7F, 0x49, 0x49, 0x49, 0x41),
    0x46: (0x7F, 0x09, 0x09, 0x09, 0x01),
    0x47: (0x3E, 0x41, 0x49, 0x49, 0x7A),
    0x48: (0x7F, 0x08, 0x08, 0x08, 0x7F),
    0x49: (0x00, 0x41, 0x7F, 0x41, 0x00),
    0x4B: (0x7F, 0x08, 0x14, 0x22, 0x41),
    0x4C: (0x7F, 0x40, 0x40, 0x40, 0x40),
    0x4D: (0x7F, 0x02, 0x0C, 0x02, 0x7F),
    0x4E: (0x7F, 0x04, 0x08, 0x10, 0x7F),
    0x4F: (0x3E, 0x41, 0x41, 0x41, 0x3E),
    0x50: (0x7F, 0x09, 0x09, 0x09, 0x06),
    0x52: (0x7F, 0x09, 0x19, 0x29, 0x46),
    0x53: (0x26, 0x49, 0x49, 0x49, 0x32),
    0x54: (0x01, 0x01, 0x7F, 0x01, 0x01),
    0x55: (0x3F, 0x40, 0x40, 0x40, 0x3F),
    0x56: (0x1F, 0x20, 0x40, 0x20, 0x1F),
    0x59: (0x07, 0x08, 0x70, 0x08, 0x07),
    0x64: (0x20, 0x54, 0x54, 0x54, 0x78),   # d
    0x65: (0x38, 0x54, 0x54, 0x54, 0x18),   # e
    0x67: (0x08, 0x54, 0x54, 0x54, 0x3C),   # g
    0x68: (0x7F, 0x08, 0x04, 0x04, 0x78),   # h
    0x6B: (0x00, 0x44, 0x7D, 0x40, 0x00),   # k
    0x6D: (0x7C, 0x04, 0x18, 0x04, 0x78),   # m
    0x6E: (0x7C, 0x08, 0x04, 0x04, 0x78),   # n
    0x6F: (0x38, 0x44, 0x44, 0x44, 0x38),   # o
    0x70: (0x7C, 0x14, 0x14, 0x14, 0x08),   # p
    0x72: (0x7C, 0x08, 0x04, 0x04, 0x08),   # r
    0x73: (0x48, 0x54, 0x54, 0x54, 0x20),   # s
    0x74: (0x04, 0x3F, 0x44, 0x44, 0x20),   # t
    0x75: (0x3C, 0x40, 0x40, 0x20, 0x7C),   # u
    0x77: (0x3C, 0x40, 0x30, 0x40, 0x3C),   # w
    0x79: (0x0C, 0x50, 0x50, 0x50, 0x3C),   # y
}

CHAR_W = 6
CHAR_H_PAGES = 1


def _i2c_write_cmd(i2c, *cmd_bytes):
    if not cmd_bytes:
        return
    data = bytearray(cmd_bytes)
    i2c.write(SSD1306_ADDR, CMD_STREAM, 1, data, len(data))


def _i2c_write_data(i2c, data):
    if not data:
        return
    n = len(data)
    if n <= I2C_DATA_CHUNK:
        i2c.write(SSD1306_ADDR, DATA_STREAM, 1, data, n)
        return
    offset = 0
    while offset < n:
        end = min(offset + I2C_DATA_CHUNK, n)
        chunk = data[offset:end]
        i2c.write(SSD1306_ADDR, DATA_STREAM, 1, chunk, len(chunk))
        offset = end
        utime.sleep_us(100)


def _set_column_page(i2c, col_start, col_end, page_start, page_end):
    _i2c_write_cmd(i2c, 0x21, col_start, col_end)
    _i2c_write_cmd(i2c, 0x22, page_start, page_end)


def _clear_rect(i2c, col_start, col_end, page_start, page_end, fill=0x00):
    _set_column_page(i2c, col_start, col_end, page_start, page_end)
    w = col_end - col_start + 1
    h = page_end - page_start + 1
    _i2c_write_data(i2c, bytearray([fill] * (w * h)))


def _draw_char(i2c, page, col, char_code):
    glyph = FONT5X7.get(char_code, FONT5X7[0x20])
    _set_column_page(i2c, col, col + 5, page, page)
    buf = bytearray(7)
    buf[0] = 0x00
    for i, b in enumerate(glyph):
        buf[1 + i] = b
    buf[6] = 0x00
    _i2c_write_data(i2c, buf)


def _draw_string(i2c, page, col_start, s):
    col = col_start
    for c in s:
        _draw_char(i2c, page, col, ord(c))
        col += CHAR_W
        if col + CHAR_W > WIDTH:
            break


# 布局常量（与参考一致：标题、经纬度、速度、类型、更新时间、电量条）
PAGE_TITLE = 0
PAGE_LAT = 1
PAGE_LON = 2
PAGE_SPD = 3
PAGE_TYPE = 4
PAGE_UPD = 5

COL_TITLE = 3
COL_LAT = 1
COL_LON = 1
COL_SPD_LABEL = 72
COL_SPD_VAL = 73
COL_TYPE = 1
COL_UPD = 1

# 各区域列宽（字符数 * 6）
LAT_MAX_CHARS = 11
LON_MAX_CHARS = 11
SPD_VAL_CHARS = 3
TYPE_MAX_CHARS = 8
UPD_MAX_CHARS = 18

BAT_COL_START = 106
BAT_COL_END = 125
BAT_PAGE_START = 0
BAT_PAGE_END = 1
BAT_SEGMENTS = 16


def _draw_battery_region(i2c, bat_cap):
    """在 (106,125) x (page 0,1) 绘制电量条：外框 + bat_cap 段 (0~16)。页内 bit0=上。"""
    w = BAT_COL_END - BAT_COL_START + 1
    n_pages = BAT_PAGE_END - BAT_PAGE_START + 1
    buf = bytearray(w * n_pages)
    # 外框：上 y=0，下 y=11，左 x=0，右 x=19
    for c in range(w):
        buf[c] |= 0x01
        buf[1 * w + c] |= (1 << (11 % 8))
    for p in range(n_pages):
        buf[p * w] |= 0xFF
        buf[p * w + (w - 1)] |= 0xFF
    # 填充段：x 1..16，y 4..11
    for i in range(min(bat_cap, BAT_SEGMENTS)):
        cx = 1 + i
        for y in range(4, 12):
            page = y // 8
            bit = y % 8
            buf[page * w + cx] |= (1 << bit)
    _set_column_page(i2c, BAT_COL_START, BAT_COL_END, BAT_PAGE_START, BAT_PAGE_END)
    _i2c_write_data(i2c, buf)


def ssd1306_init(i2c):
    _i2c_write_cmd(i2c, 0xAE)
    _i2c_write_cmd(i2c, 0xD5, 0x80)
    _i2c_write_cmd(i2c, 0xA8, 0x3F)
    _i2c_write_cmd(i2c, 0xD3, 0)
    _i2c_write_cmd(i2c, 0x40)
    _i2c_write_cmd(i2c, 0x8D, 0x14)
    _i2c_write_cmd(i2c, 0x20, 0x00)
    _i2c_write_cmd(i2c, 0xA1)
    _i2c_write_cmd(i2c, 0xC8)
    _i2c_write_cmd(i2c, 0xDA, 0x12)
    _i2c_write_cmd(i2c, 0x81, 0xCF)
    _i2c_write_cmd(i2c, 0xD9, 0xF1)
    _i2c_write_cmd(i2c, 0xDB, 0x40)
    _i2c_write_cmd(i2c, 0xA4)
    _i2c_write_cmd(i2c, 0xA6)
    _i2c_write_cmd(i2c, 0x2E)
    _i2c_write_cmd(i2c, 0xAF)


# 增量更新状态缓存
_state = {
    "init_done": False,
    "prev_lat": None,
    "prev_lon": None,
    "prev_speed": None,
    "prev_type": None,
    "prev_update": None,
    "prev_time_dif": None,
    "prev_bat": None,
}


def _draw_border(i2c):
    """绘制边框和横线（仅首次或整屏重绘时调用）。"""
    _set_column_page(i2c, 0, WIDTH - 1, 0, PAGES - 1)
    buf = bytearray(WIDTH * PAGES)
    # 上边 (0,0)-(127,0) -> page0 全部 bit0
    for c in range(WIDTH):
        buf[c] |= 0x01
    # 下边 (0,63)-(127,63) -> page7 bit7
    for c in range(WIDTH):
        buf[7 * WIDTH + c] |= 0x80
    # 左边 (0,0)-(0,63)
    for p in range(PAGES):
        buf[p * WIDTH] |= 0xFF
    # 右边 (127,0)-(127,63)
    for p in range(PAGES):
        buf[p * WIDTH + (WIDTH - 1)] |= 0xFF
    # 横线 (0,16)-(127,16) -> page2 bit0
    for c in range(WIDTH):
        buf[2 * WIDTH + c] |= 0x01
    _i2c_write_data(i2c, buf)


def _draw_static_labels(i2c):
    """绘制固定文字（仅首次调用）。"""
    _draw_string(i2c, PAGE_TITLE, COL_TITLE, "GPS APRS Inf")
    _draw_string(i2c, PAGE_SPD, COL_SPD_LABEL, "Speed:km/H")


def update_position(i2c, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=None):
    """
    增量更新 OLED：仅重绘发生变化的区域。
    lat_disp, lon_disp: 经纬度显示字符串（可截断以适配宽度）
    gnss_type: "GNSS" / "LBS" 等
    update_time: 更新时间字符串，如 "12:34"
    time_dif: 距上次秒数，如 "05"
    speed_kmh: 速度 km/h，会格式化为 3 位整数显示
    bat_pct: 电量 0~100，None 则不更新电量条
    """
    s = _state
    speed_str = "%03.0f" % (float(speed_kmh) * 1.0)
    bat_cap = round((bat_pct or 0) / 6.25) if bat_pct is not None else 0
    bat_cap = max(0, min(BAT_SEGMENTS, bat_cap))

    if not s["init_done"]:
        _draw_border(i2c)
        _draw_static_labels(i2c)
        s["init_done"] = True
        s["prev_lat"] = ""
        s["prev_lon"] = ""
        s["prev_speed"] = ""
        s["prev_type"] = ""
        s["prev_update"] = ""
        s["prev_time_dif"] = ""
        s["prev_bat"] = -1

    # 经纬度：截断到一屏能显示的字符数
    lat_disp = (lat_disp or "---")[:LAT_MAX_CHARS]
    lon_disp = (lon_disp or "---")[:LON_MAX_CHARS]
    gnss_type = (gnss_type or "---")[:TYPE_MAX_CHARS]
    upd_str = (update_time or "") + "-" + (str(time_dif) if time_dif is not None else "")

    # 仅更新变化的区域
    if lat_disp != s["prev_lat"]:
        _clear_rect(i2c, COL_LAT, COL_LAT + LAT_MAX_CHARS * CHAR_W - 1, PAGE_LAT, PAGE_LAT)
        _draw_string(i2c, PAGE_LAT, COL_LAT, lat_disp)
        s["prev_lat"] = lat_disp

    if lon_disp != s["prev_lon"]:
        _clear_rect(i2c, COL_LON, COL_LON + LON_MAX_CHARS * CHAR_W - 1, PAGE_LON, PAGE_LON)
        _draw_string(i2c, PAGE_LON, COL_LON, lon_disp)
        s["prev_lon"] = lon_disp

    if speed_str != s["prev_speed"]:
        _clear_rect(i2c, COL_SPD_VAL, COL_SPD_VAL + 3 * CHAR_W - 1, PAGE_SPD, PAGE_SPD + 1)
        _draw_string(i2c, PAGE_SPD, COL_SPD_VAL, speed_str)
        s["prev_speed"] = speed_str

    if gnss_type != s["prev_type"]:
        _clear_rect(i2c, COL_TYPE, COL_TYPE + (5 + TYPE_MAX_CHARS) * CHAR_W - 1, PAGE_TYPE, PAGE_TYPE)
        _draw_string(i2c, PAGE_TYPE, COL_TYPE, "Type:" + gnss_type)
        s["prev_type"] = gnss_type

    if upd_str != (s["prev_update"] + "-" + s["prev_time_dif"]):
        _clear_rect(i2c, COL_UPD, COL_UPD + UPD_MAX_CHARS * CHAR_W - 1, PAGE_UPD, PAGE_UPD)
        _draw_string(i2c, PAGE_UPD, COL_UPD, ("Upd:" + upd_str)[:UPD_MAX_CHARS])
        s["prev_update"] = update_time or ""
        s["prev_time_dif"] = str(time_dif) if time_dif is not None else ""

    if bat_pct is not None and bat_cap != s["prev_bat"]:
        _draw_battery_region(i2c, bat_cap)
        s["prev_bat"] = bat_cap


def init_oled():
    """初始化 I2C 与 SSD1306，返回 i2c 对象；失败返回 None。"""
    try:
        i2c = I2C(I2C_PORT, I2C_MODE)
        utime.sleep_ms(50)
        ssd1306_init(i2c)
        utime.sleep_ms(50)
        return i2c
    except Exception as e:
        print("oled_display init_oled error:", e)
        return None
