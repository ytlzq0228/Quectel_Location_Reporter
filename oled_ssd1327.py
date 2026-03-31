# oled_ssd1327.py - SSD1327 128x128 SPI 4bpp 显示驱动（布局与 SSD1306 版一致，内容区占纵向 0–63 像素）
import utime
import log
from machine import SPI, Pin

from oled_common import (
    font_small,
    font_large,
    SMALL_H_PAGES,
    glyph_to_column_major,
    measure_number_cols as _measure_number_cols,
    first_last_diff as _first_last_diff,
    format_ago as _format_ago,
    format_lat_3d4_ns as _format_lat_3d4_ns,
    format_lon_3d4_ew as _format_lon_3d4_ew,
)

_log = log.getLogger("OLED")

WIDTH = 128
HEIGHT = 128
COL_BYTES = WIDTH // 2
FRAME_BYTES = COL_BYTES * HEIGHT
CHUNK = 8192
PAGES = 16

spi = None
pin_dc = None
pin_cs = None
pin_rst = None
pin_boost = None  # VPP(12V) 升压使能，与 VCI/VDD 逻辑供电独立；高=开，None=不控制
_boost_power_cut = False
_fb = None
_fb_ok = False
_spi_timing_debug = False
_spi_timing_verbose = False
_prepare_debug = False  # update_display 内 prepare 分段 ms（oled_prepare_debug）
_spi_dbg_fb = None  # 上次 _write_framebuffer：(setup_us, data_us, span_us, nbytes, chunks)
_HAS_TICKS_US = hasattr(utime, "ticks_us")

# (id(font), ch) -> (bytearray 列主序缓冲, 字宽 w, h_pages)；避免每次绘制都跑 glyph_to_column_major
_col_major_cache = {}

# init_oled 时预热；缺字时 _get_col_major_buf 仍会懒填充
_GLYPH_WARM_CHARS = (
    "0123456789 "
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    ":,.-/_+\"'()[]<>?=*#@%!&"
    ">"
)


def _get_col_major_buf(font, ch):
    """返回 (buf, w, h_pages)；buf 为 glyph_to_column_major 结果，按 (font, 单字) 缓存。"""
    if not ch:
        return None
    ch = ch[0]
    key = (id(font), ch)
    ent = _col_major_cache.get(key)
    if ent is not None:
        return ent
    try:
        glyph, h, w = font.get_ch(ch)
    except Exception:
        if ch == "?":
            return None
        ent = _get_col_major_buf(font, "?")
        if ent is None:
            return None
        _col_major_cache[key] = ent
        return ent
    buf = glyph_to_column_major(glyph, w, h)
    h_pages = (h + 7) // 8
    ent = (buf, w, h_pages)
    _col_major_cache[key] = ent
    return ent


def _warm_col_major_cache_at_boot():
    """SSD1327 初始化成功后调用：常用字符列主序转好放内存，首帧绘制不再转换。"""
    for fnt in (font_small, font_large):
        for ch in _GLYPH_WARM_CHARS:
            try:
                _get_col_major_buf(fnt, ch)
            except Exception:
                pass
        try:
            _get_col_major_buf(fnt, "?")
        except Exception:
            pass
    try:
        _log.info("oled_ssd1327: glyph col-major cache keys=%d" % len(_col_major_cache))
    except Exception:
        pass

DEFAULT_REMAP = 0x51
DEFAULT_FUNCTION_SEL_A = 0x00


def _gray_byte(gray4):
    g = gray4 & 0x0F
    return (g << 4) | g


def _time_start():
    return utime.ticks_us() if _HAS_TICKS_US else utime.ticks_ms()


def _time_diff(start):
    if _HAS_TICKS_US:
        return utime.ticks_diff(utime.ticks_us(), start), "us"
    return utime.ticks_diff(utime.ticks_ms(), start), "ms"


def _time_start_ms():
    """构图/清屏等可能 >32ms，勿用 ticks_us（部分平台 16 位会回绕导致 prepare 假大）。"""
    return utime.ticks_ms()


def _time_diff_ms(start_ms):
    return utime.ticks_diff(utime.ticks_ms(), start_ms)


def _fb_fill_uniform(byte_v):
    """整帧填充同一字节（仅改内存，不送屏）。切页时用其替代 clear+flush，避免双次全帧 SPI。
    按行 + 8 字节展开，减少 Python 层循环次数（原 8192 次单索引迭代在部分固件上要数百 ms～1s+）。"""
    b = byte_v & 0xFF
    fb = _fb
    stride = COL_BYTES
    yy = 0
    while yy < HEIGHT:
        base = yy * stride
        j = 0
        while j + 8 <= stride:
            o = base + j
            fb[o] = b
            fb[o + 1] = b
            fb[o + 2] = b
            fb[o + 3] = b
            fb[o + 4] = b
            fb[o + 5] = b
            fb[o + 6] = b
            fb[o + 7] = b
            j += 8
        while j < stride:
            fb[base + j] = b
            j += 1
        yy += 1


def _spi_write(buf):
    """SPI.write 见 QuecPython 文档：成功 0，失败 -1；部分环境要求可写 buffer。"""
    if isinstance(buf, bytes):
        buf = bytearray(buf)
    elif not isinstance(buf, bytearray):
        buf = bytearray(buf)
    n = len(buf)
    if n == 0:
        return
    if _spi_timing_debug and _spi_timing_verbose:
        t0 = _time_start()
    ret = spi.write(buf, n)
    if _spi_timing_debug and _spi_timing_verbose:
        dt, unit = _time_diff(t0)
        _log.info("ssd1327 SPI write n=%d %s=%d" % (n, unit, dt))
    if ret == 0:
        return
    raise OSError("spi.write failed ret=%s" % ret)


def _write_cmd(cmd):
    pin_cs.write(0)
    pin_dc.write(0)
    _spi_write(bytes([cmd & 0xFF]))
    pin_cs.write(1)


def _write_cmd_data(cmd, data_bytes):
    pin_cs.write(0)
    pin_dc.write(0)
    _spi_write(bytes([cmd & 0xFF]))
    if data_bytes:
        n = len(data_bytes)
        off = 0
        while off < n:
            end = min(off + CHUNK, n)
            _spi_write(data_bytes[off:end])
            off = end
    pin_cs.write(1)


def _set_window_full_in_cs():
    pin_dc.write(0)
    _spi_write(b"\x15\x00\x7F")
    _spi_write(b"\x75\x00\x7F")
    _spi_write(b"\x5C")
    pin_dc.write(1)


def _write_framebuffer(fb):
    global _spi_dbg_fb
    if len(fb) != FRAME_BYTES:
        raise ValueError("frame size")
    n = len(fb)
    off = 0
    chunks = 0
    if _spi_timing_debug:
        t_fb = _time_start()
    pin_cs.write(0)
    if _spi_timing_debug:
        t_setup = _time_start()
    _set_window_full_in_cs()
    if _spi_timing_debug:
        setup_us, u_s = _time_diff(t_setup)
        t_data = _time_start()
    while off < n:
        end = min(off + CHUNK, n)
        _spi_write(fb[off:end])
        chunks += 1
        off = end
    if _spi_timing_debug:
        data_us, u_d = _time_diff(t_data)
    pin_cs.write(1)
    if _spi_timing_debug:
        total_us, u_t = _time_diff(t_fb)
        _spi_dbg_fb = (setup_us, data_us, total_us, n, chunks)


def _reset_panel():
    pin_rst.write(1)
    utime.sleep_ms(10)
    pin_rst.write(0)
    utime.sleep_ms(30)
    pin_rst.write(1)
    utime.sleep_ms(120)


def _init_panel(remap, func_a):
    _reset_panel()
    _write_cmd_data(0xFD, b"\x12")
    _write_cmd(0xAE)
    _write_cmd_data(0x15, b"\x00\x7F")
    _write_cmd_data(0x75, b"\x00\x7F")
    _write_cmd_data(0x81, b"\xFF")
    _write_cmd_data(0xA0, bytes([remap & 0xFF]))
    _write_cmd_data(0xA1, b"\x00")
    _write_cmd_data(0xA2, b"\x00")
    _write_cmd(0xA4)
    _write_cmd(0xA6)
    _write_cmd_data(0xA8, b"\x7F")
    _write_cmd_data(0xB1, b"\xF1")
    _write_cmd_data(0xB3, b"\x00")
    _write_cmd_data(0xAB, bytes([func_a & 0xFF]))
    _write_cmd(0xB9)
    _write_cmd_data(0xB6, b"\x0F")
    _write_cmd_data(0xBE, b"\x0F")
    _write_cmd_data(0xBC, b"\x08")
    _write_cmd_data(0xD5, b"\x62")
    utime.sleep_ms(120)
    _write_cmd(0xAF)
    utime.sleep_ms(50)


def fb_put_pixel(fb, x, y, gray4):
    if x < 0 or x >= WIDTH or y < 0 or y >= HEIGHT:
        return
    g = gray4 & 0x0F
    idx = y * COL_BYTES + (x >> 1)
    b = fb[idx]
    if x & 1:
        fb[idx] = (b & 0xF0) | g
    else:
        fb[idx] = (g << 4) | (b & 0x0F)


def fb_fill_rect(fb, x0, y0, w, h, gray4):
    """按行写字节，避免逐像素 fb_put_pixel（boot/清行快一个数量级）。"""
    g = gray4 & 0x0F
    pair_b = (g << 4) | g
    x1 = min(x0 + w, WIDTH)
    y1 = min(y0 + h, HEIGHT)
    x0 = max(x0, 0)
    y0 = max(y0, 0)
    if x0 >= x1 or y0 >= y1:
        return
    for yy in range(y0, y1):
        row = yy * COL_BYTES
        x = x0
        while x < x1:
            bi = row + (x >> 1)
            if (x & 1) == 0 and (x + 1) < x1:
                fb[bi] = pair_b
                x += 2
            else:
                bb = fb[bi]
                if (x & 1) == 0:
                    fb[bi] = (g << 4) | (bb & 0x0F)
                else:
                    fb[bi] = (bb & 0xF0) | g
                x += 1


def _fb_fill_rect_pages(fb, col_start, col_end, page_start, page_end, fill_byte):
    gray = 0x0F if fill_byte else 0x00
    y0 = page_start * 8
    w = col_end - col_start + 1
    h = (page_end - page_start + 1) * 8
    fb_fill_rect(fb, col_start, y0, w, h, gray)


def fb_blit_column_major(fb, col_px, y_top, buf, cols, h_pages, fg=0x0F, bg=0x00):
    """列主序 1bpp → SSD1327 4bpp 交错 nibbles。热点路径内联写 _fb，避免逐像素 fb_put_pixel 调用。"""
    fg &= 0x0F
    bg &= 0x0F
    rs = COL_BYTES
    y_lim = HEIGHT
    for c in range(cols):
        x = col_px + c
        if x < 0 or x >= WIDTH:
            continue
        xi = x >> 1
        x_odd = x & 1
        for p in range(h_pages):
            b = buf[p * cols + c]
            y0 = y_top + (p << 3)
            if y0 >= y_lim:
                break
            base = y0 * rs + xi
            for bit in range(8):
                y = y0 + bit
                if y < 0:
                    continue
                if y >= y_lim:
                    break
                g = fg if (b >> bit) & 1 else bg
                idx = base + bit * rs
                bb = fb[idx]
                if x_odd:
                    fb[idx] = (bb & 0xF0) | g
                else:
                    fb[idx] = (g << 4) | (bb & 0x0F)


def _draw_char(page, col, ch, font):
    ent = _get_col_major_buf(font, ch)
    if ent is None:
        try:
            return font.max_width() + 1
        except Exception:
            return 1
    buf, w, h_pages = ent
    cols = w + 1
    y_top = page * 8
    fb_blit_column_major(_fb, col, y_top, buf, cols, h_pages, 0x0F, 0x00)
    return w + 1


def _draw_string(page, col, s, font, max_col=None):
    if max_col is None:
        max_col = WIDTH
    for c in s:
        if col >= max_col:
            break
        adv = _draw_char(page, col, c, font)
        col += adv


def _draw_number(page, col, s, font):
    for c in s:
        if col >= WIDTH:
            break
        adv = _draw_char(page, col, c, font)
        col += adv
    return col


def _draw_number_right(page, right_col, s, font):
    width_cols = _measure_number_cols(s, font)
    start_col = right_col - width_cols + 1
    if start_col < 0:
        start_col = 0
    _draw_number(page, start_col, s, font)


def _blit_one_column(page, col, col_bytes):
    y_top = page * 8
    fb_blit_column_major(_fb, col, y_top, col_bytes, 1, len(col_bytes), 0x0F, 0x00)


def _draw_hline(y, x0=0, x1=WIDTH - 1, gray4=0x05):
    """在 _fb 上绘制一条 1px 水平线，用于标题栏/底栏分隔。"""
    if _fb is None:
        return
    fb_fill_rect(_fb, x0, y, x1 - x0 + 1, 1, gray4)


def _draw_char_fg(page, col, ch, font, fg=0x0F, bg=0x00):
    """带自定义前景/背景灰度的单字符绘制。"""
    ent = _get_col_major_buf(font, ch)
    if ent is None:
        try:
            return font.max_width() + 1
        except Exception:
            return 1
    buf, w, h_pages = ent
    cols = w + 1
    y_top = page * 8
    fb_blit_column_major(_fb, col, y_top, buf, cols, h_pages, fg, bg)
    return w + 1


def _draw_string_fg(page, col, s, font, max_col=None, fg=0x0F, bg=0x00):
    """带灰度的字符串绘制；标签用暗色（如 0x07），数值用亮色（0x0F）。"""
    if max_col is None:
        max_col = WIDTH
    for c in s:
        if col >= max_col:
            break
        adv = _draw_char_fg(page, col, c, font, fg, bg)
        col += adv


def _draw_static_frame():
    """模式切换后绘制固定装饰元素：两条分隔线 + 底栏 km/h 单位标签。"""
    _draw_hline(DIVIDER_Y1, 0, WIDTH - 1, 0x05)
    _draw_hline(DIVIDER_Y2, 0, WIDTH - 1, 0x05)
    _draw_string_fg(PAGE_BOTTOM_BAR, 2, "km/h", font_small, fg=0x07)


BAT_COL_START = 116
BAT_COL_END = 127
BAT_PAGE_START = 0
BAT_PAGE_END = 1
BAT_SEGMENTS = 8


def _draw_battery(seg_count):
    w = BAT_COL_END - BAT_COL_START + 1
    np = BAT_PAGE_END - BAT_PAGE_START + 1
    buf = bytearray(w * np)

    def set_pixel(c, y):
        if 0 <= c < w and 0 <= y < 16:
            p = y // 8
            bit = y % 8
            buf[p * w + c] |= 1 << bit

    for cx in range(9):
        set_pixel(cx, 2)
        set_pixel(cx, 10)
    for ry in range(2, 11):
        set_pixel(0, ry)
        set_pixel(8, ry)
    for cx in range(10, 12):
        for ry in range(4, 9):
            set_pixel(cx, ry)
    seg = max(0, min(seg_count, BAT_SEGMENTS))
    for i in range(seg):
        for ry in range(3, 10):
            set_pixel(1 + i, ry)

    y_base = BAT_PAGE_START * 8
    for c in range(w):
        col_buf = bytearray([buf[p * w + c] for p in range(np)])
        fb_blit_column_major(_fb, BAT_COL_START + c, y_base, col_buf, 1, np, 0x0F, 0x00)


# 主界面 / update_position：标题 p0-1；分隔线 y=16(p2)；内容+速度 p3-12；分隔线 y=104(p13)；底栏 p14-15
PAGE_TITLE = 0
PAGE_LAT = 3
PAGE_LON = 5          # 原 6，上移一页
PAGE_TYPE_UPD = 7     # 原 9，移至速度区下方可全宽显示
PAGE_SPD_START = 3
PAGE_SPD_END = 7      # 不含，速度占 p3-6 = 32px（不变）

COL_TITLE = 2
COL_LAT = 1
COL_LON = 1
COL_TYPE_UPD = 1
COL_LEFT_MAX = 62     # 原 54，速度在右侧约 col 63-127

LAT_MAX_CH = 11
LON_MAX_CH = 11
TYPE_MAX_CH = 6
UPD_MAX_CH = 10
TYPE_UPD_MAX_CH = 18

SPD_COL_RIGHT = WIDTH - 1

# 分隔线与底栏
DIVIDER_Y1 = 16       # 标题栏下方 1px 分隔线（p2 顶部像素）
DIVIDER_Y2 = 104      # 底栏上方 1px 分隔线（p13 顶部像素）
PAGE_BOTTOM_BAR = 14  # 底栏页（p14-15，y=112-127）：km/h 标签 + 速度进度条
SPD_BAR_COL_START = 28   # 进度条起始列（"km/h" 标签右侧）
SPD_BAR_COL_END = WIDTH - 1  # 进度条结束列
SPD_BAR_MAX_KMH = 150    # 进度条满格对应速度（km/h）

PAGE_C_TITLE = 0
PAGE_C_SPD_START = 3
PAGE_C_SPD_END = 7
PAGE_C_TYPE = 3
PAGE_C_APRS = 5       # 原 6，上移
PAGE_C_TRACCAR = 7    # 原 9，移至速度区下方
PAGE_C_ACCU = 9       # 新增精度行
COL_C_TITLE = 2
COL_C_LEFT = 1
COL_C_LEFT_MAX = 62   # 原 54
C_TITLE_LEN = 10
C_TYPE_LEN = 11
C_LINE_LEN = 18

SPD_COL_RIGHT_C = WIDTH - 1

BOOT_TITLE_PAGES = 2      # 标题栏占 p0-1（y=0-15）
BOOT_MSG_START_PAGE = 2   # 消息区从 p2（y=16）开始，紧接标题栏
BOOT_MAX_LINES = min(7, (PAGES - BOOT_TITLE_PAGES) // SMALL_H_PAGES) if PAGES > BOOT_TITLE_PAGES else 1
BOOT_CHARS_PER_LINE = 20
_state_boot = []

PAGE_LINE0 = 0
PAGE_LINE1 = 3
PAGE_LINE2 = 5        # 原 6，上移
PAGE_LINE3 = 7        # 原 9，速度区下方（可全宽）
PAGE_LINE4 = 9        # 新增
PAGE_LINE5 = 11       # 新增
CONTENT_COL_END = COL_LEFT_MAX       # 速度区左侧内容边界（p3-6）
CONTENT_COL_END_FULL = WIDTH - 1     # 速度区下方全宽内容边界（p7+）
CONTENT_MAX_CH = 20

_state_multi = {
    "display_mode": -1,
    "prev_bat": None,
    "prev_speed": None,
    "prev_line0": None,
    "prev_line1": None,
    "prev_line2": None,
    "prev_line3": None,
    "prev_line4": None,
    "prev_line5": None,
    "prev_sats": None,
    "prev_bar_fill_w": None,
    "prev_spd_bar_fill_w": None,
    "oled_error_logged": False,
}

BAR_BOTTOM_INSET_PX = 4
BAR_LAST_PAGE_MASK = 0x0F
BAR_EMPTY_LAST_ROW = 0x08


def _draw_progress_bar(page, col_start, col_end, percent_0_100, prev_fill_w=None):
    pct = max(0, min(100, int(percent_0_100)))
    inner_start = col_start + 1
    inner_end = col_end - 1
    inner_w = max(0, inner_end - inner_start + 1)
    fill_w = (inner_w * pct) // 100
    page_end = page + SMALL_H_PAGES - 1
    if SMALL_H_PAGES <= 1:
        filled_col_bytes = bytearray([BAR_LAST_PAGE_MASK])
        empty_col_bytes = bytearray([0x01 | BAR_EMPTY_LAST_ROW])
    else:
        filled_col_bytes = bytearray([0xFF] * (SMALL_H_PAGES - 1) + [BAR_LAST_PAGE_MASK])
        empty_col_bytes = bytearray([0x01] + [0x00] * (SMALL_H_PAGES - 2) + [BAR_EMPTY_LAST_ROW])

    def draw_col_filled(col):
        _blit_one_column(page, col, filled_col_bytes)

    def draw_col_empty(col):
        _blit_one_column(page, col, empty_col_bytes)

    if prev_fill_w is None:
        draw_col_filled(col_start)
        draw_col_filled(col_end)
        for i in range(inner_w):
            col = inner_start + i
            if i < fill_w:
                draw_col_filled(col)
            else:
                draw_col_empty(col)
    else:
        lo = min(fill_w, prev_fill_w)
        hi = max(fill_w, prev_fill_w)
        for i in range(lo, hi):
            if i >= inner_w:
                break
            col = inner_start + i
            if i < fill_w:
                draw_col_filled(col)
            else:
                draw_col_empty(col)
    return fill_w


def _draw_content_line_incremental(page, prev_text, new_text, font, max_col=None, content_col_end=None):
    col_right = content_col_end if content_col_end is not None else CONTENT_COL_END
    new = (new_text or "")[:CONTENT_MAX_CH]
    if prev_text is None:
        _fb_fill_rect_pages(_fb, COL_TITLE, col_right, page, page + SMALL_H_PAGES - 1, 0x00)
        _draw_string(page, COL_TITLE, new, font, max_col=max_col or WIDTH)
        return
    old = (prev_text or "")[:CONTENT_MAX_CH]
    if old == new:
        return
    first, last = _first_last_diff(old, new)
    if last < first:
        return
    col_start = COL_TITLE + _measure_number_cols(new[:first], font)
    w_old = _measure_number_cols(old[first : last + 1], font)
    w_new = _measure_number_cols(new[first : last + 1], font)
    clear_w = max(w_old, w_new)
    col_end = col_start + clear_w - 1
    clear_end = min(col_end, col_right)
    if col_start <= clear_end:
        _fb_fill_rect_pages(_fb, col_start, clear_end, page, page + SMALL_H_PAGES - 1, 0x00)
    substr = new[first : last + 1]
    if substr:
        _draw_string(page, col_start, substr, font, max_col=max_col or WIDTH)


_state = {
    "init_done": False,
    "prev_lat": None,
    "prev_lon": None,
    "prev_speed": None,
    "prev_type_upd": None,
    "prev_bat": None,
    "prev_spd_bar_fill_w": None,
    "oled_error_logged": False,
}

_state_compact = {
    "init_done": False,
    "prev_title": None,
    "prev_bat": None,
    "prev_speed": None,
    "prev_type": None,
    "prev_aprs_ago": None,
    "prev_traccar_ago": None,
    "prev_accuracy": None,
    "prev_spd_bar_fill_w": None,
    "oled_error_logged": False,
}


def _pin_from_num(n):
    return getattr(Pin, "GPIO%d" % int(n))


def _cfg_int(cfg, key, default):
    if not cfg:
        return default
    try:
        v = cfg.get(key)
        if v is None or str(v).strip() == "":
            return default
        return int(str(v).strip(), 0)
    except Exception:
        return default


def _power_restore_vpp_and_display_on():
    """仅关断 VPP(12V) 时调用：逻辑供电 VCI/VDD 仍在，拉高升压后发 0xAF 即可，无需整片 re-init。"""
    global _boost_power_cut
    if pin_boost is None or not _boost_power_cut:
        return
    try:
        pin_boost.write(1)
        utime.sleep_ms(50)
        _write_cmd(0xAF)
    except Exception as e:
        _log.warning("oled_ssd1327 VPP restore: %s" % e)
    _boost_power_cut = False
    _state_multi["display_mode"] = -1


def init_oled(oled_cfg=None):
    """初始化 SPI 与 SSD1327；成功返回 True，失败返回 None。oled_cfg 为 dict（来自配置文件）。"""
    global spi, pin_dc, pin_cs, pin_rst, pin_boost, _fb, _fb_ok
    global _boost_power_cut, _spi_timing_debug, _spi_timing_verbose, _prepare_debug
    cfg = oled_cfg or {}
    _spi_timing_debug = _cfg_int(cfg, "spi_timing_debug", 0) != 0
    _spi_timing_verbose = _cfg_int(cfg, "spi_timing_verbose", 0) != 0
    _prepare_debug = _cfg_int(cfg, "prepare_debug", 0) != 0
    try:
        boost_gpio = _cfg_int(cfg, "boost_gpio", 20)
        if boost_gpio < 0:
            pin_boost = None
        else:
            pin_boost = Pin(_pin_from_num(boost_gpio), Pin.OUT, Pin.PULL_DISABLE, 1)
            utime.sleep_ms(30)

        port = _cfg_int(cfg, "spi_port", 0)
        mode = _cfg_int(cfg, "spi_mode", 0)
        clk = _cfg_int(cfg, "spi_clk", 4)
        group = _cfg_int(cfg, "spi_group", 1)
        gpio_rst = _cfg_int(cfg, "gpio_rst", 5)
        gpio_dc = _cfg_int(cfg, "gpio_dc", 7)
        gpio_cs = _cfg_int(cfg, "gpio_cs", 8)
        remap = _cfg_int(cfg, "remap", DEFAULT_REMAP)
        func_a = _cfg_int(cfg, "function_sel_a", DEFAULT_FUNCTION_SEL_A)
        _boost_power_cut = False

        spi = SPI(port, mode, clk, group)
        pin_rst = Pin(_pin_from_num(gpio_rst), Pin.OUT, Pin.PULL_DISABLE, 1)
        pin_dc = Pin(_pin_from_num(gpio_dc), Pin.OUT, Pin.PULL_DISABLE, 0)
        pin_cs = Pin(_pin_from_num(gpio_cs), Pin.OUT, Pin.PULL_DISABLE, 1)

        _fb = bytearray(FRAME_BYTES)
        for i in range(FRAME_BYTES):
            _fb[i] = 0
        _init_panel(remap, func_a)
        _fb_ok = True
        # 上电后必须把整帧写入 GRAM，否则部分屏一直黑/花屏（单独跑 test 时 write_patterns 会写屏）
        try:
            _write_framebuffer(_fb)
            if _spi_timing_debug and _spi_dbg_fb is not None:
                su, du, tu, bn, ch = _spi_dbg_fb
                _log.info(
                    "ssd1327 init_fb span_us=%d setup_us=%d data_us=%d bytes=%d chunks=%d"
                    % (tu, su, du, bn, ch)
                )
        except Exception as e:
            _log.warning("oled_ssd1327: initial framebuffer push: %s" % e)
        _warm_col_major_cache_at_boot()
        if _prepare_debug:
            _log.info("oled_ssd1327: prepare_debug=1 (OLED_prepare detail lines enabled)")
        return True
    except Exception as e:
        _log.error("oled_ssd1327 init_oled error: %s" % e)
        _fb_ok = False
        try:
            if pin_boost is not None:
                pin_boost.write(1)
        except Exception:
            pass
        try:
            if spi is not None:
                spi.close()
        except Exception:
            pass
        spi = None
        return None


def _alive(ctx):
    return ctx is not None and _fb_ok and _fb is not None


def _flush(compose_prepare_ms=None):
    """compose_prepare_ms：进入 _flush 前 Python 构图/写 _fb 耗时（ms，ticks_ms），仅排障。"""
    if _fb is not None and _fb_ok:
        a4_us = 0
        if _spi_timing_debug:
            t_flush = _time_start()
        if _boost_power_cut:
            _power_restore_vpp_and_display_on()
        # 避免被其它命令切到 A5 整屏测模式后 GRAM 不刷新
        if _spi_timing_debug:
            t_a4 = _time_start()
        try:
            _write_cmd(0xA4)
        except Exception:
            pass
        if _spi_timing_debug:
            a4_us, _ = _time_diff(t_a4)
        _write_framebuffer(_fb)
        if _spi_timing_debug:
            flush_wall_us, _ = _time_diff(t_flush)
            fb = _spi_dbg_fb
            if fb:
                su, du, tu, bn, ch = fb
            else:
                su = du = tu = bn = ch = 0
            if compose_prepare_ms is not None:
                _log.info(
                    "ssd1327 refresh prepare_ms=%d spi_wall_us=%d a4_us=%d fb_setup_us=%d fb_data_us=%d fb_span_us=%d bytes=%d chunks=%d"
                    % (compose_prepare_ms, flush_wall_us, a4_us, su, du, tu, bn, ch)
                )
            else:
                _log.info(
                    "ssd1327 refresh spi_wall_us=%d a4_us=%d fb_setup_us=%d fb_data_us=%d fb_span_us=%d bytes=%d chunks=%d"
                    % (flush_wall_us, a4_us, su, du, tu, bn, ch)
                )


def set_brightness(ctx, percent):
    if not _alive(ctx):
        return
    if _boost_power_cut:
        return
    try:
        pct = max(1, min(100, int(percent) if percent is not None else 100))
        contrast = (pct * 255) // 100
        _write_cmd_data(0x81, bytes([contrast & 0xFF]))
    except Exception as e:
        _log.error("oled_ssd1327 set_brightness error: %s" % e)


def clear(ctx, fill=0x00):
    global _state_boot
    if not _alive(ctx):
        return
    try:
        prep_ms = None
        if _spi_timing_debug:
            t_prep_ms = _time_start_ms()
        _fb_fill_uniform(_gray_byte(0x0F if fill else 0x00))
        if _spi_timing_debug:
            prep_ms = _time_diff_ms(t_prep_ms)
        _flush(compose_prepare_ms=prep_ms)
        _state_boot = []
        _state_multi["display_mode"] = -1
    except Exception as e:
        _log.error("oled_ssd1327 clear error: %s" % e)


def _draw_boot_header():
    """绘制 boot 页面标题栏：深灰底色 + 'BOOT' 标题 + 底部分隔线。"""
    fb_fill_rect(_fb, 0, 0, WIDTH, BOOT_TITLE_PAGES * 8, 0x02)
    _draw_string_fg(0, 3, "BOOT", font_small, fg=0x0F, bg=0x02)
    _draw_hline(BOOT_TITLE_PAGES * 8 - 1, 0, WIDTH - 1, 0x06)


def show_boot_message(ctx, msg="Booting..."):
    global _state_boot
    if not _alive(ctx):
        return
    try:
        prep_ms = None
        if _spi_timing_debug:
            t_prep_ms = _time_start_ms()
        line = str(msg)[:BOOT_CHARS_PER_LINE]
        n = len(_state_boot)
        msg_area_end = BOOT_MSG_START_PAGE + BOOT_MAX_LINES * SMALL_H_PAGES - 1
        if n < BOOT_MAX_LINES:
            if n == 0:
                # 首条消息：绘制标题栏并清空整个消息区
                _draw_boot_header()
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, BOOT_MSG_START_PAGE, msg_area_end, 0x00)
            _state_boot.append(line)
            row = len(_state_boot) - 1
            page_start = BOOT_MSG_START_PAGE + row * SMALL_H_PAGES
            if n > 0:
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
            _draw_string(page_start, 2, line, font_small)
        else:
            _state_boot = _state_boot[1:] + [line]
            for row in range(BOOT_MAX_LINES):
                page_start = BOOT_MSG_START_PAGE + row * SMALL_H_PAGES
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
                _draw_string(page_start, 2, _state_boot[row], font_small)
        if _spi_timing_debug:
            prep_ms = _time_diff_ms(t_prep_ms)
        _flush(compose_prepare_ms=prep_ms)
    except Exception as e:
        _log.error("oled_ssd1327 show_boot_message error: %s" % e)


def update_menu_cursor(ctx, prev_row, new_row):
    if not _alive(ctx) or prev_row == new_row:
        return
    try:
        prep_ms = None
        if _spi_timing_debug:
            t_prep_ms = _time_start_ms()
        w = max(_measure_number_cols("  ", font_small), _measure_number_cols("> ", font_small))
        col_end = min(w - 1, WIDTH - 1)
        for r, prefix in ((prev_row, "  "), (new_row, "> ")):
            page_start = r * SMALL_H_PAGES
            _fb_fill_rect_pages(_fb, 0, col_end, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
            _draw_string(page_start, 0, prefix, font_small)
        if _spi_timing_debug:
            prep_ms = _time_diff_ms(t_prep_ms)
        _flush(compose_prepare_ms=prep_ms)
    except Exception as e:
        _log.error("oled_ssd1327 update_menu_cursor error: %s" % e)


def update_display(
    ctx,
    display_mode,
    speed_kmh,
    bat_pct=None,
    lat_disp=None,
    lon_disp=None,
    gnss_type=None,
    aprs_ago_sec=None,
    traccar_ago_sec=None,
    system_time_str=None,
    accuracy_m=None,
    heading=None,
    sats=None,
):
    global _boost_power_cut
    try:
        if not _alive(ctx):
            return
        sm = _state_multi

        if display_mode == 3:
            if sm["display_mode"] != 3:
                _write_cmd(0xAE)
                utime.sleep_ms(10)
                if pin_boost is not None:
                    pin_boost.write(0)
                    _boost_power_cut = True
                sm["display_mode"] = 3
            return

        prep_ms = None
        t0_prep = None
        t_lap = None
        if _spi_timing_debug or _prepare_debug:
            t0_prep = _time_start_ms()

        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))

        ms_head = 0
        if _prepare_debug and t0_prep is not None:
            ms_head = _time_diff_ms(t0_prep)
            t_lap = _time_start_ms()

        ms_wake = 0
        if sm["display_mode"] == 3:
            if _boost_power_cut:
                _power_restore_vpp_and_display_on()
            else:
                _write_cmd(0xAF)
        if _prepare_debug and t_lap is not None:
            ms_wake = _time_diff_ms(t_lap)
            t_lap = _time_start_ms()

        ms_mode_sw = 0
        ms_fill_uniform = 0
        if sm["display_mode"] != display_mode:
            # 勿在此调用 clear()：clear 会 _flush 整帧，随后本函数末尾再 _flush，切页会双倍 SPI。
            if _prepare_debug and t_lap is not None:
                t_fill0 = _time_start_ms()
            _fb_fill_uniform(_gray_byte(0x00))
            if _prepare_debug and t_lap is not None:
                ms_fill_uniform = _time_diff_ms(t_fill0)
            _draw_static_frame()
            _state_boot = []
            sm["display_mode"] = display_mode
            sm["prev_line0"] = None
            sm["prev_line1"] = None
            sm["prev_line2"] = None
            sm["prev_line3"] = None
            sm["prev_line4"] = None
            sm["prev_line5"] = None
            sm["prev_sats"] = None
            sm["prev_bar_fill_w"] = None
            sm["prev_spd_bar_fill_w"] = None
            sm["prev_speed"] = None
            sm["prev_bat"] = None
        if _prepare_debug and t_lap is not None:
            ms_mode_sw = _time_diff_ms(t_lap)
            t_lap = _time_start_ms()

        if bat_pct is not None and bat_seg != sm["prev_bat"]:
            _draw_battery(bat_seg)
            sm["prev_bat"] = bat_seg
        if speed_str != sm["prev_speed"]:
            prev_spd = sm["prev_speed"]
            if prev_spd is None:
                w = _measure_number_cols(speed_str, font_large)
                spd_start = max(CONTENT_COL_END + 1, SPD_COL_RIGHT - w + 1)
                _fb_fill_rect_pages(_fb, spd_start, SPD_COL_RIGHT, PAGE_SPD_START, PAGE_SPD_END - 1, 0x00)
                _draw_number_right(PAGE_SPD_START, SPD_COL_RIGHT, speed_str, font_large)
            else:
                first, last = _first_last_diff(prev_spd, speed_str)
                if last >= first:
                    w = _measure_number_cols(speed_str, font_large)
                    spd_base = SPD_COL_RIGHT - w + 1
                    if spd_base < CONTENT_COL_END + 1:
                        spd_base = CONTENT_COL_END + 1
                    col_start = spd_base + _measure_number_cols(speed_str[:first], font_large)
                    w_old = _measure_number_cols(prev_spd[first : last + 1], font_large)
                    w_new = _measure_number_cols(speed_str[first : last + 1], font_large)
                    col_end = col_start + max(w_old, w_new) - 1
                    col_end = min(col_end, SPD_COL_RIGHT)
                    if col_start <= col_end:
                        _fb_fill_rect_pages(_fb, col_start, col_end, PAGE_SPD_START, PAGE_SPD_END - 1, 0x00)
                    substr = speed_str[first : last + 1]
                    if substr:
                        _draw_number(PAGE_SPD_START, col_start, substr, font_large)
                else:
                    w = _measure_number_cols(speed_str, font_large)
                    spd_start = max(CONTENT_COL_END + 1, SPD_COL_RIGHT - w + 1)
                    _fb_fill_rect_pages(_fb, spd_start, SPD_COL_RIGHT, PAGE_SPD_START, PAGE_SPD_END - 1, 0x00)
                    _draw_number_right(PAGE_SPD_START, SPD_COL_RIGHT, speed_str, font_large)
            sm["prev_speed"] = speed_str
        # 底栏速度进度条：仅速度变化或首次绘制时更新，增量刷新开销极小
        if speed_str != sm.get("_last_bar_speed") or sm["prev_spd_bar_fill_w"] is None:
            spd_bar_pct = min(100, int(round(float(speed_kmh or 0))) * 100 // SPD_BAR_MAX_KMH)
            fill_w = _draw_progress_bar(
                PAGE_BOTTOM_BAR, SPD_BAR_COL_START, SPD_BAR_COL_END,
                spd_bar_pct, prev_fill_w=sm["prev_spd_bar_fill_w"]
            )
            sm["prev_spd_bar_fill_w"] = fill_w
            sm["_last_bar_speed"] = speed_str
        ms_bat_spd = 0
        if _prepare_debug and t_lap is not None:
            ms_bat_spd = _time_diff_ms(t_lap)
            t_lap = _time_start_ms()

        if display_mode == 0:
            line0 = "GNSS INFO"
            line1 = _format_lat_3d4_ns(lat_disp)
            line2 = _format_lon_3d4_ew(lon_disp)
            line3 = "Type:" + (gnss_type or "---")
            if accuracy_m is not None:
                try:
                    line4 = "Acc:%dm" % min(9999, max(0, int(round(float(accuracy_m)))))
                except (TypeError, ValueError):
                    line4 = "Acc: --"
            else:
                line4 = "Acc: --"
            if heading is not None:
                try:
                    line5 = "HDG:%d" % (int(round(float(heading))) % 360)
                except (TypeError, ValueError):
                    line5 = "HDG: --"
            else:
                line5 = "HDG: --"
        elif display_mode == 1:
            line0 = "Report"
            line1 = "AU:" + _format_ago(aprs_ago_sec)
            line2 = "TU:" + _format_ago(traccar_ago_sec)
            try:
                loc = utime.localtime()
                line3 = "%04d-%02d-%02d" % (loc[0], loc[1], loc[2])
                line4 = "%02d:%02d:%02d" % (loc[3], loc[4], loc[5])
            except Exception:
                line3 = (system_time_str or "--:--:--")
                line4 = ""
            line5 = ""
        else:
            line0 = "Acc/HDG/SAT"
            if accuracy_m is not None:
                try:
                    line1 = "Acc:" + "%3d" % min(999, max(0, int(round(float(accuracy_m)))))
                except (TypeError, ValueError):
                    line1 = "Acc: --"
            else:
                line1 = "Acc: --"
            if heading is not None:
                try:
                    line2 = "HDG:" + "%3d" % min(999, max(0, int(round(float(heading)))))
                except (TypeError, ValueError):
                    line2 = "HDG: --"
            else:
                line2 = "HDG: --"
            line3 = None
            line4 = ""
            line5 = ""

        if display_mode == 2:
            try:
                _sats_val = max(0, min(50, int(sats))) if sats is not None else 0
            except (TypeError, ValueError):
                _sats_val = 0
        else:
            _sats_val = 0
        ms_fmt_lines = 0
        if _prepare_debug and t_lap is not None:
            ms_fmt_lines = _time_diff_ms(t_lap)
            t_lap = _time_start_ms()

        if line0 != sm["prev_line0"]:
            _draw_content_line_incremental(PAGE_LINE0, sm["prev_line0"], line0, font_small)
            sm["prev_line0"] = line0
            if bat_pct is not None:
                _draw_battery(bat_seg)

        if line1 != sm["prev_line1"]:
            _draw_content_line_incremental(PAGE_LINE1, sm["prev_line1"], line1, font_small)
            sm["prev_line1"] = line1

        if line2 != sm["prev_line2"]:
            _draw_content_line_incremental(PAGE_LINE2, sm["prev_line2"], line2, font_small)
            sm["prev_line2"] = line2

        if display_mode == 2:
            sat_label = "SAT:" + "%02d" % _sats_val
            bar_start = COL_TITLE + _measure_number_cols(sat_label, font_small) + 5
            pct = (_sats_val * 100) // 50
            if sat_label != sm["prev_line3"]:
                _draw_content_line_incremental(
                    PAGE_LINE3, sm["prev_line3"], sat_label, font_small,
                    max_col=bar_start - 1, content_col_end=bar_start - 1
                )
                sm["prev_line3"] = sat_label
            fill_w = _draw_progress_bar(
                PAGE_LINE3, bar_start, WIDTH - 1, pct, prev_fill_w=sm.get("prev_bar_fill_w")
            )
            sm["prev_sats"] = _sats_val
            sm["prev_bar_fill_w"] = fill_w
        else:
            if line3 != sm["prev_line3"]:
                _draw_content_line_incremental(
                    PAGE_LINE3, sm["prev_line3"], line3, font_small,
                    content_col_end=CONTENT_COL_END_FULL
                )
                sm["prev_line3"] = line3
            sm["prev_sats"] = None
            sm["prev_bar_fill_w"] = None

        if line4 != sm["prev_line4"]:
            _draw_content_line_incremental(
                PAGE_LINE4, sm["prev_line4"], line4, font_small,
                content_col_end=CONTENT_COL_END_FULL
            )
            sm["prev_line4"] = line4

        if line5 != sm["prev_line5"]:
            _draw_content_line_incremental(
                PAGE_LINE5, sm["prev_line5"], line5, font_small,
                content_col_end=CONTENT_COL_END_FULL
            )
            sm["prev_line5"] = line5

        ms_draw = 0
        if _prepare_debug and t_lap is not None:
            ms_draw = _time_diff_ms(t_lap)

        sm["oled_error_logged"] = False
        if _prepare_debug and t0_prep is not None:
            tot = _time_diff_ms(t0_prep)
            _log.info(
                "OLED_prepare detail dm=%d head_ms=%d wake_ms=%d fill_u_ms=%d mode_sw_ms=%d bat_spd_ms=%d fmt_lines_ms=%d draw_ms=%d total_ms=%d"
                % (
                    display_mode,
                    ms_head,
                    ms_wake,
                    ms_fill_uniform,
                    ms_mode_sw,
                    ms_bat_spd,
                    ms_fmt_lines,
                    ms_draw,
                    tot,
                )
            )
        if _spi_timing_debug or _prepare_debug:
            if t0_prep is not None:
                prep_ms = _time_diff_ms(t0_prep)
        _flush(compose_prepare_ms=prep_ms)
    except Exception as e:
        if not _state_multi.get("oled_error_logged"):
            _log.warning("oled_ssd1327 update_display: %s" % e)
            _state_multi["oled_error_logged"] = True


def _draw_static_labels():
    _draw_string(PAGE_TITLE, COL_TITLE, "GNSS INFO", font_small)


def update_position(ctx, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=None):
    try:
        if not _alive(ctx):
            return
        prep_ms = None
        if _spi_timing_debug:
            t_prep_ms = _time_start_ms()
        s = _state
        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))

        if not s["init_done"]:
            _draw_static_labels()
            _draw_static_frame()
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
            _fb_fill_rect_pages(_fb, COL_LAT, COL_LEFT_MAX, PAGE_LAT, PAGE_LAT + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_LAT, COL_LAT, lat_disp, font_small)
            s["prev_lat"] = lat_disp

        if lon_disp != s["prev_lon"]:
            _fb_fill_rect_pages(_fb, COL_LON, COL_LEFT_MAX, PAGE_LON, PAGE_LON + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_LON, COL_LON, lon_disp, font_small)
            s["prev_lon"] = lon_disp

        if speed_str != s["prev_speed"]:
            w = _measure_number_cols(speed_str, font_large)
            spd_start = max(COL_LEFT_MAX + 1, SPD_COL_RIGHT - w + 1)
            _fb_fill_rect_pages(_fb, spd_start, SPD_COL_RIGHT, PAGE_SPD_START, PAGE_SPD_END - 1, 0x00)
            _draw_number_right(PAGE_SPD_START, SPD_COL_RIGHT, speed_str, font_large)
            s["prev_speed"] = speed_str
        if speed_str != s.get("_last_bar_speed") or s["prev_spd_bar_fill_w"] is None:
            spd_bar_pct = min(100, int(round(float(speed_kmh or 0))) * 100 // SPD_BAR_MAX_KMH)
            fill_w = _draw_progress_bar(
                PAGE_BOTTOM_BAR, SPD_BAR_COL_START, SPD_BAR_COL_END,
                spd_bar_pct, prev_fill_w=s["prev_spd_bar_fill_w"]
            )
            s["prev_spd_bar_fill_w"] = fill_w
            s["_last_bar_speed"] = speed_str

        if type_upd_line != (s.get("prev_type_upd") or ""):
            _fb_fill_rect_pages(_fb, COL_TYPE_UPD, CONTENT_COL_END_FULL, PAGE_TYPE_UPD, PAGE_TYPE_UPD + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_TYPE_UPD, COL_TYPE_UPD, type_upd_line, font_small)
            s["prev_type_upd"] = type_upd_line

        if bat_pct is not None and bat_seg != s["prev_bat"]:
            _draw_battery(bat_seg)
            s["prev_bat"] = bat_seg

        s["oled_error_logged"] = False
        if _spi_timing_debug:
            prep_ms = _time_diff_ms(t_prep_ms)
        _flush(compose_prepare_ms=prep_ms)
    except Exception as e:
        if not s.get("oled_error_logged"):
            _log.warning("oled_ssd1327 update_position: %s" % e)
            s["oled_error_logged"] = True


def reset_display_compact():
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
        sc["prev_spd_bar_fill_w"] = None
    except Exception as e:
        _log.error("oled_ssd1327 reset_display_compact error: %s" % e)


def _draw_compact_static(title):
    _draw_string(PAGE_C_TITLE, COL_C_TITLE, (title or "Quec GNSS")[:C_TITLE_LEN], font_small)


def update_display_compact(
    ctx,
    title="Quec GNSS",
    bat_pct=None,
    speed_kmh=None,
    gnss_type=None,
    aprs_ago_sec=None,
    traccar_ago_sec=None,
    accuracy_m=None,
):
    try:
        if not _alive(ctx):
            return
        prep_ms = None
        if _spi_timing_debug:
            t_prep_ms = _time_start_ms()
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
        line_aprs = ("AU:" + aprs_str)[:C_LINE_LEN]
        line_trcr = ("TU:" + traccar_str)[:C_LINE_LEN]
        line_accu = ("Acc:" + acc_str)[:C_LINE_LEN]

        if not sc["init_done"]:
            _draw_compact_static(title)
            _draw_static_frame()
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
            _fb_fill_rect_pages(_fb, COL_C_TITLE, COL_C_LEFT_MAX, PAGE_C_TITLE, PAGE_C_TITLE + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_TITLE, COL_C_TITLE, title_disp, font_small)
            sc["prev_title"] = title_disp

        if bat_pct is not None and bat_seg != sc["prev_bat"]:
            _draw_battery(bat_seg)
            sc["prev_bat"] = bat_seg

        if speed_str != sc["prev_speed"]:
            w = _measure_number_cols(speed_str, font_large)
            spd_start = max(COL_C_LEFT_MAX + 1, SPD_COL_RIGHT_C - w + 1)
            _fb_fill_rect_pages(_fb, spd_start, SPD_COL_RIGHT_C, PAGE_C_SPD_START, PAGE_C_SPD_END - 1, 0x00)
            _draw_number_right(PAGE_C_SPD_START, SPD_COL_RIGHT_C, speed_str, font_large)
            sc["prev_speed"] = speed_str
        if speed_str != sc.get("_last_bar_speed") or sc["prev_spd_bar_fill_w"] is None:
            spd_bar_pct = min(100, int(round(float(speed_kmh or 0))) * 100 // SPD_BAR_MAX_KMH)
            fill_w = _draw_progress_bar(
                PAGE_BOTTOM_BAR, SPD_BAR_COL_START, SPD_BAR_COL_END,
                spd_bar_pct, prev_fill_w=sc["prev_spd_bar_fill_w"]
            )
            sc["prev_spd_bar_fill_w"] = fill_w
            sc["_last_bar_speed"] = speed_str

        type_disp = ("Type:" + type_str)[:C_TYPE_LEN]
        if type_disp != sc["prev_type"]:
            _fb_fill_rect_pages(_fb, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TYPE, PAGE_C_TYPE + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_TYPE, COL_C_LEFT, type_disp, font_small)
            sc["prev_type"] = type_disp

        if line_aprs != sc["prev_aprs_ago"]:
            _fb_fill_rect_pages(_fb, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_APRS, PAGE_C_APRS + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_APRS, COL_C_LEFT, line_aprs, font_small)
            sc["prev_aprs_ago"] = line_aprs

        if line_trcr != sc["prev_traccar_ago"]:
            _fb_fill_rect_pages(_fb, COL_C_LEFT, CONTENT_COL_END_FULL, PAGE_C_TRACCAR, PAGE_C_TRACCAR + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_TRACCAR, COL_C_LEFT, line_trcr, font_small)
            sc["prev_traccar_ago"] = line_trcr

        if line_accu != sc["prev_accuracy"]:
            _fb_fill_rect_pages(_fb, COL_C_LEFT, CONTENT_COL_END_FULL, PAGE_C_ACCU, PAGE_C_ACCU + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_ACCU, COL_C_LEFT, line_accu, font_small)
            sc["prev_accuracy"] = line_accu

        sc["oled_error_logged"] = False
        if _spi_timing_debug:
            prep_ms = _time_diff_ms(t_prep_ms)
        _flush(compose_prepare_ms=prep_ms)
    except Exception as e:
        if not sc.get("oled_error_logged"):
            _log.warning("oled_ssd1327 update_display_compact: %s" % e)
            sc["oled_error_logged"] = True
