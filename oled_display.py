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

# 12x24 大号数字字模（速度等关键数据用），每数字 12 列 x 24 行 = 36 字节，列优先每列 3 字节 (行 0-7,8-15,16-23)
# 7 段加粗风格，高对比度
def _col3(b0, b1, b2):
    return (b0, b1, b2)

def _digit_12x24_data(*cols):
    out = bytearray(36)
    for i, (b0, b1, b2) in enumerate(cols):
        out[i * 3] = b0
        out[i * 3 + 1] = b1
        out[i * 3 + 2] = b2
    return out

# 0: 外框  1: 右竖  2: 上+右上+中+左下+下  3: 上+右上+中+右下+下  4: 左上+右上+中+右下
# 5: 上+左上+中+右下+下  6: 上+左上+中+左下+右下+下  7: 上+右上+右下  8: 全  9: 上+左上+右上+中+右下+下
_TOP, _BOT = 0x07, 0xE0
_MID = 0x1C
_LT, _RT = 0xFF, 0xFF  # 左竖全、右竖全 (单字节段)
_LT_TOP, _LT_BOT = 0xF8, 0x1F
_RT_TOP, _RT_BOT = 0x1F, 0xF8
FONT_12X24 = [
    _digit_12x24_data(  # 0
        _col3(0xFF, 0xFF, 0xFF), _col3(0xFF, 0xFF, 0xFF),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(0xFF, 0xFF, 0xFF), _col3(0xFF, 0xFF, 0xFF),
    ),
    _digit_12x24_data(  # 1
        _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00),
        _col3(0xFF, 0xFF, 0xFF), _col3(0xFF, 0xFF, 0xFF), _col3(0xFF, 0xFF, 0xFF), _col3(0xFF, 0xFF, 0xFF),
        _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00),
    ),
    _digit_12x24_data(  # 2
        _col3(_TOP, 0x00, 0xFF), _col3(_TOP, 0x00, 0xFF),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
        _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
    ),
    _digit_12x24_data(  # 3
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT),
        _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
    ),
    _digit_12x24_data(  # 4
        _col3(0x00, 0x00, 0xFF), _col3(0x00, 0x00, 0xFF),
        _col3(0x00, 0x00, 0xFF), _col3(0x00, 0x00, 0xFF), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT),
        _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(0x00, 0x00, 0xFF), _col3(0x00, 0x00, 0xFF),
        _col3(0x00, 0x00, 0xFF), _col3(0x00, 0x00, 0xFF),
    ),
    _digit_12x24_data(  # 5
        _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
        _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
    ),
    _digit_12x24_data(  # 6
        _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
        _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0xFF, 0x00), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
    ),
    _digit_12x24_data(  # 7
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(0x00, 0xFF, 0xFF), _col3(0x00, 0xFF, 0xFF),
        _col3(0x00, 0xFF, 0xFF), _col3(0x00, 0xFF, 0xFF), _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00),
        _col3(0x00, 0x00, 0x00), _col3(0x00, 0x00, 0x00),
    ),
    _digit_12x24_data(  # 8
        _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT),
        _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
    ),
    _digit_12x24_data(  # 9
        _col3(_TOP, 0xFF, 0xFF), _col3(_TOP, 0xFF, 0xFF),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0xFF, _BOT), _col3(_TOP, 0xFF, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
        _col3(_TOP, 0x00, _BOT), _col3(_TOP, 0x00, _BOT),
    ),
]

LARGE_DIGIT_W = 12
LARGE_DIGIT_H_PAGES = 3

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
    0x51: (0x3E, 0x41, 0x41, 0x45, 0x3A),   # Q（O 带右下尾）
    0x52: (0x7F, 0x09, 0x19, 0x29, 0x46),
    0x53: (0x26, 0x49, 0x49, 0x49, 0x32),
    0x54: (0x01, 0x01, 0x7F, 0x01, 0x01),
    0x55: (0x3F, 0x40, 0x40, 0x40, 0x3F),
    0x56: (0x1F, 0x20, 0x40, 0x20, 0x1F),
    0x59: (0x07, 0x08, 0x70, 0x08, 0x07),
    0x63: (0x38, 0x44, 0x40, 0x40, 0x38),   # c
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
    """绘制 5x7 字符，占 6 列（左 1 空 + 5 字宽），避免与下一字重叠。"""
    glyph = FONT5X7.get(char_code, FONT5X7[0x20])
    _set_column_page(i2c, col, col + 5, page, page)
    buf = bytearray(6)
    buf[0] = 0x00
    for i in range(5):
        buf[1 + i] = glyph[i]
    _i2c_write_data(i2c, buf)


def _draw_string(i2c, page, col_start, s):
    col = col_start
    for c in s:
        _draw_char(i2c, page, col, ord(c))
        col += CHAR_W
        if col + CHAR_W > WIDTH:
            break


def _draw_large_digit(i2c, col_start, page_start, digit_byte):
    """绘制一个 12x24 大号数字（0-9）。字模列优先每列 3 字节(行0-7,8-15,16-23)，页内 bit0=上，与多数 SSD1306 一致。"""
    idx = digit_byte - 0x30
    if idx < 0 or idx > 9:
        idx = 0
    font_data = FONT_12X24[idx]
    # 转为页优先：SSD1306 水平寻址下连续写为 (col0,page0)..(col11,page0),(col0,page1)..
    page_major = bytearray(36)
    for p in range(LARGE_DIGIT_H_PAGES):
        for c in range(LARGE_DIGIT_W):
            page_major[p * LARGE_DIGIT_W + c] = font_data[c * 3 + p]
    _set_column_page(i2c, col_start, col_start + LARGE_DIGIT_W - 1, page_start, page_start + LARGE_DIGIT_H_PAGES - 1)
    _i2c_write_data(i2c, page_major)


def _draw_large_number(i2c, col_start, page_start, s):
    """绘制大号数字字符串（如 '000'），仅绘制 0-9。"""
    col = col_start
    for c in s:
        if 0x30 <= ord(c) <= 0x39:
            _draw_large_digit(i2c, col, page_start, ord(c))
            col += LARGE_DIGIT_W
        if col + LARGE_DIGIT_W > WIDTH:
            break


# 布局常量（参考之前样子：左侧小字信息，右侧大号速度；无边框更简洁）
PAGE_TITLE = 0
PAGE_LAT = 1
PAGE_LON = 2
PAGE_SPD_LARGE_START = 2   # 大号速度占用 page 2,3,4（24 行）
PAGE_SPD_LARGE_END = 5
PAGE_TYPE = 5
PAGE_UPD = 6

COL_TITLE = 2
COL_LAT = 1
COL_LON = 1
COL_SPD_LABEL = 72        # 小字 "Speed:km/h" 在右侧上方
COL_SPD_VAL = 76          # 大号三位数起始列，12*3=36 宽，76+36=112
COL_TYPE = 1
COL_UPD = 1
COL_LEFT_MAX = 71   # 左侧内容最大列，右侧 72+ 为速度标签与大号速度

# 各区域列宽（字符数 * 6）
LAT_MAX_CHARS = 11
LON_MAX_CHARS = 11
SPD_VAL_CHARS = 3
TYPE_MAX_CHARS = 8
UPD_MAX_CHARS = 18

# 大号速度区列/页范围（用于增量清除）
SPD_LARGE_COL_END = COL_SPD_VAL + 3 * LARGE_DIGIT_W - 1

BAT_COL_START = 106
BAT_COL_END = 125
BAT_PAGE_START = 0
BAT_PAGE_END = 1
BAT_SEGMENTS = 16


def _draw_battery_region(i2c, bat_cap):
    """在 (106,125) x (page 0,1) 绘制电池图标：主体矩形 + 顶部正极凸起 + 内部电量段 (0~16)。页内 MSB=上，与小字一致。"""
    w = BAT_COL_END - BAT_COL_START + 1  # 20
    n_pages = BAT_PAGE_END - BAT_PAGE_START + 1  # 2
    buf = bytearray(w * n_pages)
    # 逻辑行 y=0(上)..15(下)：page0 为 y=0..7，page1 为 y=8..15；SSD1306 页内 bit7=上
    def set_pixel(c, y):
        if 0 <= c < w and 0 <= y < 16:
            page = y // 8
            bit = 7 - (y % 8)
            buf[page * w + c] |= (1 << bit)
    # 顶部凸起：小矩形 (8,0)-(11,2)
    for cx in range(8, 12):
        for ry in range(0, 3):
            set_pixel(cx, ry)
    # 主体外框：左竖 x=0, 右竖 x=19, 上横 y=3, 下横 y=11
    for cx in range(w):
        set_pixel(cx, 3)
        set_pixel(cx, 11)
    for ry in range(3, 12):
        set_pixel(0, ry)
        set_pixel(19, ry)
    # 电量填充：主体内 x=1..18, y=4..10，按 bat_cap 填左侧 1..(1+seg)
    seg = min(bat_cap, BAT_SEGMENTS)
    for i in range(seg):
        cx = 1 + i
        for ry in range(4, 11):
            set_pixel(cx, ry)
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
    "oled_error_logged": False,  # 未接/断开屏幕时仅打印一次错误，避免刷屏
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
    """绘制固定文字（仅首次调用）：左侧标题，右侧 Speed 标签（小字）。"""
    _draw_string(i2c, PAGE_TITLE, COL_TITLE, "GPS APRS Inf")
    _draw_string(i2c, PAGE_TITLE, COL_SPD_LABEL, "Speed:km/h")


# ------------------------- 紧凑布局（无经纬度，含 APRS/Traccar 距上次上报、精度）-------------------------
# 布局：P0=标题+电量, P1=Speed 标签, P2-4=大号速度, P5=定位方式, P6=APRS/精度, P7=Traccar
PAGE_C_TITLE = 0
PAGE_C_SPD_LABEL = 1
PAGE_C_SPD_START = 2
PAGE_C_SPD_END = 5
PAGE_C_TYPE = 5
PAGE_C_APRS_ACC = 6
PAGE_C_TRACCAR = 7
COL_C_TITLE = 2
COL_C_SPD_LABEL = 72
COL_C_SPD_VAL = 76
COL_C_LEFT = 1
COL_C_LEFT_MAX = 71
C_TITLE_LEN = 10
C_TYPE_LEN = 11
C_LINE_LEN = 18

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


def _format_ago(seconds):
    """将秒数格式化为短字符串，如 5s、1m02s、1h01m。"""
    if seconds is None or seconds < 0:
        return "--"
    s = int(seconds)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def _draw_compact_static(i2c, title):
    """紧凑布局下仅绘制固定标题与 Speed 标签。"""
    _draw_string(i2c, PAGE_C_TITLE, COL_C_TITLE, (title or "Quec GNSS")[:C_TITLE_LEN])
    _draw_string(i2c, PAGE_C_SPD_LABEL, COL_C_SPD_LABEL, "Speed:km/h")


def reset_display_compact():
    """重置紧凑布局的缓存状态。清屏后再次显示前调用，否则会认为已初始化而不重绘标题等。"""
    sc = _state_compact
    sc["init_done"] = False
    sc["prev_title"] = None
    sc["prev_bat"] = None
    sc["prev_speed"] = None
    sc["prev_type"] = None
    sc["prev_aprs_ago"] = None
    sc["prev_traccar_ago"] = None
    sc["prev_accuracy"] = None


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
    紧凑布局增量更新：标题、电量、速度、定位方式、距上次上报 APRS/Traccar 时间、精度。
    不显示经纬度。未接屏或 I2C 失败时静默返回。
    aprs_ago_sec / traccar_ago_sec: 距上次上报的秒数（整数或浮点），None 显示为 --。
    accuracy_m: 精度米数，None 显示为 --。
    """
    if i2c is None:
        return
    sc = _state_compact
    speed_str = "%03.0f" % (float(speed_kmh or 0) * 1.0)
    bat_cap = round((bat_pct or 0) / 6.25) if bat_pct is not None else 0
    bat_cap = max(0, min(BAT_SEGMENTS, bat_cap))
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
    line_aprs_acc = ("APRS:%s Acc:%s" % (aprs_str, acc_str))[:C_LINE_LEN]
    line_traccar = ("Trcr:%s" % traccar_str)[:C_LINE_LEN]

    try:
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
            _clear_rect(i2c, COL_C_TITLE, COL_C_TITLE + C_TITLE_LEN * CHAR_W - 1, PAGE_C_TITLE, PAGE_C_TITLE)
            _draw_string(i2c, PAGE_C_TITLE, COL_C_TITLE, title_disp)
            sc["prev_title"] = title_disp

        if bat_pct is not None and bat_cap != sc["prev_bat"]:
            _draw_battery_region(i2c, bat_cap)
            sc["prev_bat"] = bat_cap

        if speed_str != sc["prev_speed"]:
            _clear_rect(i2c, COL_C_SPD_VAL, SPD_LARGE_COL_END, PAGE_C_SPD_START, PAGE_C_SPD_END - 1)
            _draw_large_number(i2c, COL_C_SPD_VAL, PAGE_C_SPD_START, speed_str)
            sc["prev_speed"] = speed_str

        type_disp = ("Type:" + type_str)[:C_TYPE_LEN]
        if type_disp != sc["prev_type"]:
            _clear_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TYPE, PAGE_C_TYPE)
            _draw_string(i2c, PAGE_C_TYPE, COL_C_LEFT, type_disp)
            sc["prev_type"] = type_disp

        if line_aprs_acc != sc["prev_aprs_ago"]:
            _clear_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_APRS_ACC, PAGE_C_APRS_ACC)
            _draw_string(i2c, PAGE_C_APRS_ACC, COL_C_LEFT, line_aprs_acc)
            sc["prev_aprs_ago"] = line_aprs_acc

        if line_traccar != sc["prev_traccar_ago"]:
            _clear_rect(i2c, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TRACCAR, PAGE_C_TRACCAR)
            _draw_string(i2c, PAGE_C_TRACCAR, COL_C_LEFT, line_traccar)
            sc["prev_traccar_ago"] = line_traccar

        sc["oled_error_logged"] = False
    except (OSError, Exception) as e:
        if not sc.get("oled_error_logged"):
            print("oled_display (compact): I2C error:", e)
            sc["oled_error_logged"] = True


def update_position(i2c, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=None):
    """
    增量更新 OLED：仅重绘发生变化的区域。
    未接或断开屏幕时（i2c 为 None 或 I2C 写入失败）会静默返回，不抛异常。
    lat_disp, lon_disp: 经纬度显示字符串（可截断以适配宽度）
    gnss_type: "GNSS" / "LBS" 等
    update_time: 更新时间字符串，如 "12:34"
    time_dif: 距上次秒数，如 "05"
    speed_kmh: 速度 km/h，会格式化为 3 位整数显示
    bat_pct: 电量 0~100，None 则不更新电量条
    """
    if i2c is None:
        return
    s = _state
    speed_str = "%03.0f" % (float(speed_kmh) * 1.0)
    bat_cap = round((bat_pct or 0) / 6.25) if bat_pct is not None else 0
    bat_cap = max(0, min(BAT_SEGMENTS, bat_cap))

    try:
        if not s["init_done"]:
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
            _clear_rect(i2c, COL_SPD_VAL, SPD_LARGE_COL_END, PAGE_SPD_LARGE_START, PAGE_SPD_LARGE_END - 1)
            _draw_large_number(i2c, COL_SPD_VAL, PAGE_SPD_LARGE_START, speed_str)
            s["prev_speed"] = speed_str

        if gnss_type != s["prev_type"]:
            _clear_rect(i2c, COL_TYPE, COL_LEFT_MAX, PAGE_TYPE, PAGE_TYPE)
            _draw_string(i2c, PAGE_TYPE, COL_TYPE, ("Type:" + gnss_type)[:11])
            s["prev_type"] = gnss_type

        if upd_str != (s["prev_update"] + "-" + s["prev_time_dif"]):
            _clear_rect(i2c, COL_UPD, COL_LEFT_MAX, PAGE_UPD, PAGE_UPD)
            _draw_string(i2c, PAGE_UPD, COL_UPD, ("Upd:" + upd_str)[:11])
            s["prev_update"] = update_time or ""
            s["prev_time_dif"] = str(time_dif) if time_dif is not None else ""

        if bat_pct is not None and bat_cap != s["prev_bat"]:
            _draw_battery_region(i2c, bat_cap)
            s["prev_bat"] = bat_cap

        s["oled_error_logged"] = False
    except (OSError, Exception) as e:
        if not s.get("oled_error_logged"):
            print("oled_display: I2C error (screen not connected?):", e)
            s["oled_error_logged"] = True


def clear(i2c, fill=0x00):
    """整屏填充为黑（fill=0x00）或白（0xFF）。程序退出时调用可黑屏。"""
    if i2c is None:
        return
    try:
        _set_column_page(i2c, 0, WIDTH - 1, 0, PAGES - 1)
        buf = bytearray([fill] * (WIDTH * PAGES))
        _i2c_write_data(i2c, buf)
    except Exception as e:
        print("oled_display clear error:", e)


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
