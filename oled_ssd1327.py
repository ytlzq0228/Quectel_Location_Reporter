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
    format_ago_sec_only as _format_ago_sec_only,
    format_lat_3d4_ns as _format_lat_3d4_ns,
    format_lon_3d4_ew as _format_lon_3d4_ew,
)

_log = log.getLogger("OLED")

WIDTH = 128
HEIGHT = 128
COL_BYTES = WIDTH // 2
FRAME_BYTES = COL_BYTES * HEIGHT
CHUNK = 256
# 物理高度 128px = 16 页（每页 8px）。布局在 64px 方案基础上增加行间留白页。
PAGES = 16

spi = None
pin_dc = None
pin_cs = None
pin_rst = None
pin_boost = None  # VPP(12V) 升压使能，与 VCI/VDD 逻辑供电独立；高=开，None=不控制
_boost_power_cut = False
_fb = None
_fb_ok = False

DEFAULT_REMAP = 0x51
DEFAULT_FUNCTION_SEL_A = 0x00


def _gray_byte(gray4):
    g = gray4 & 0x0F
    return (g << 4) | g


def _spi_write(buf):
    """SPI.write 见 QuecPython 文档：成功 0，失败 -1；部分环境要求可写 buffer。"""
    if isinstance(buf, bytes):
        buf = bytearray(buf)
    elif not isinstance(buf, bytearray):
        buf = bytearray(buf)
    n = len(buf)
    if n == 0:
        return
    ret = spi.write(buf, n)
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
    if len(fb) != FRAME_BYTES:
        raise ValueError("frame size")
    pin_cs.write(0)
    _set_window_full_in_cs()
    n = len(fb)
    off = 0
    while off < n:
        end = min(off + CHUNK, n)
        _spi_write(fb[off:end])
        off = end
    pin_cs.write(1)


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
    g = gray4 & 0x0F
    x1 = min(x0 + w, WIDTH)
    y1 = min(y0 + h, HEIGHT)
    x0 = max(x0, 0)
    y0 = max(y0, 0)
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            fb_put_pixel(fb, xx, yy, g)


def _fb_fill_rect_pages(fb, col_start, col_end, page_start, page_end, fill_byte):
    gray = 0x0F if fill_byte else 0x00
    y0 = page_start * 8
    w = col_end - col_start + 1
    h = (page_end - page_start + 1) * 8
    fb_fill_rect(fb, col_start, y0, w, h, gray)


def fb_blit_column_major(fb, col_px, y_top, buf, cols, h_pages, fg=0x0F, bg=0x00):
    fg &= 0x0F
    bg &= 0x0F
    for c in range(cols):
        for p in range(h_pages):
            b = buf[p * cols + c]
            for bit in range(8):
                x = col_px + c
                y = y_top + p * 8 + bit
                fb_put_pixel(fb, x, y, fg if ((b >> bit) & 1) else bg)


def _draw_char(page, col, ch, font):
    try:
        glyph, h, w = font.get_ch(ch)
    except Exception:
        try:
            glyph, h, w = font.get_ch("?")
        except Exception:
            return font.max_width() + 1
    buf = glyph_to_column_major(glyph, w, h)
    cols = w + 1
    h_pages = (h + 7) // 8
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


# 主界面 / update_position：标题 p0-1；p2 留白；经纬度 p3-4 / p6-7；类型行 p9-10；大号速度占 p3-6（与经纬区对齐）
PAGE_TITLE = 0
PAGE_LAT = 3
PAGE_LON = 6
PAGE_TYPE_UPD = 9
PAGE_SPD_START = 3
PAGE_SPD_END = 7

COL_TITLE = 2
COL_LAT = 1
COL_LON = 1
COL_TYPE_UPD = 1
COL_LEFT_MAX = 54

LAT_MAX_CH = 11
LON_MAX_CH = 11
TYPE_MAX_CH = 6
UPD_MAX_CH = 10
TYPE_UPD_MAX_CH = 18

SPD_COL_RIGHT = WIDTH - 1

PAGE_C_TITLE = 0
PAGE_C_SPD_START = 3
PAGE_C_SPD_END = 7
PAGE_C_TYPE = 3
PAGE_C_APRS = 6
PAGE_C_TRACCAR = 9
COL_C_TITLE = 2
COL_C_LEFT = 1
COL_C_LEFT_MAX = 54
C_TITLE_LEN = 10
C_TYPE_LEN = 11
C_LINE_LEN = 18

SPD_COL_RIGHT_C = WIDTH - 1

BOOT_MAX_LINES = min(6, PAGES // SMALL_H_PAGES) if PAGES >= SMALL_H_PAGES else 1
BOOT_CHARS_PER_LINE = 21
_state_boot = []

PAGE_LINE0 = 0
PAGE_LINE1 = 3
PAGE_LINE2 = 6
PAGE_LINE3 = 9
CONTENT_COL_END = COL_LEFT_MAX
CONTENT_MAX_CH = 18

_state_multi = {
    "display_mode": -1,
    "prev_bat": None,
    "prev_speed": None,
    "prev_line0": None,
    "prev_line1": None,
    "prev_line2": None,
    "prev_line3": None,
    "prev_sats": None,
    "prev_bar_fill_w": None,
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
    global _boost_power_cut
    cfg = oled_cfg or {}
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
        except Exception as e:
            _log.warning("oled_ssd1327: initial framebuffer push: %s" % e)
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


def _flush():
    if _fb is not None and _fb_ok:
        if _boost_power_cut:
            _power_restore_vpp_and_display_on()
        # 避免被其它命令切到 A5 整屏测模式后 GRAM 不刷新
        try:
            _write_cmd(0xA4)
        except Exception:
            pass
        _write_framebuffer(_fb)


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
        v = _gray_byte(0x0F if fill else 0x00)
        for i in range(FRAME_BYTES):
            _fb[i] = v
        _flush()
        _state_boot = []
        _state_multi["display_mode"] = -1
    except Exception as e:
        _log.error("oled_ssd1327 clear error: %s" % e)


def show_boot_message(ctx, msg="Booting..."):
    global _state_boot
    if not _alive(ctx):
        return
    try:
        line = str(msg)[:BOOT_CHARS_PER_LINE]
        n = len(_state_boot)
        if n < BOOT_MAX_LINES:
            if n == 0:
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, 0, BOOT_MAX_LINES * SMALL_H_PAGES - 1, 0x00)
            _state_boot.append(line)
            row = len(_state_boot) - 1
            page_start = row * SMALL_H_PAGES
            if n > 0:
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
            _draw_string(page_start, 0, line, font_small)
        else:
            _state_boot = _state_boot[1:] + [line]
            for row in range(BOOT_MAX_LINES):
                page_start = row * SMALL_H_PAGES
                _fb_fill_rect_pages(_fb, 0, WIDTH - 1, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
                _draw_string(page_start, 0, _state_boot[row], font_small)
        _flush()
    except Exception as e:
        _log.error("oled_ssd1327 show_boot_message error: %s" % e)


def update_menu_cursor(ctx, prev_row, new_row):
    if not _alive(ctx) or prev_row == new_row:
        return
    try:
        w = max(_measure_number_cols("  ", font_small), _measure_number_cols("> ", font_small))
        col_end = min(w - 1, WIDTH - 1)
        for r, prefix in ((prev_row, "  "), (new_row, "> ")):
            page_start = r * SMALL_H_PAGES
            _fb_fill_rect_pages(_fb, 0, col_end, page_start, page_start + SMALL_H_PAGES - 1, 0x00)
            _draw_string(page_start, 0, prefix, font_small)
        _flush()
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

        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))

        if sm["display_mode"] == 3:
            if _boost_power_cut:
                _power_restore_vpp_and_display_on()
            else:
                _write_cmd(0xAF)

        if sm["display_mode"] != display_mode:
            clear(ctx, 0x00)
            sm["display_mode"] = display_mode
            sm["prev_line0"] = None
            sm["prev_line1"] = None
            sm["prev_line2"] = None
            sm["prev_line3"] = None
            sm["prev_sats"] = None
            sm["prev_bar_fill_w"] = None
            sm["prev_speed"] = None
            _fb_fill_rect_pages(_fb, 0, CONTENT_COL_END, 0, PAGES - 1, 0x00)

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

        if display_mode == 0:
            line0 = "GNSS INFO"
            line1 = _format_lat_3d4_ns(lat_disp)
            line2 = _format_lon_3d4_ew(lon_disp)
            line3 = "Type:" + (gnss_type or "---")
        elif display_mode == 1:
            line0 = "Report Status"
            line1 = "AU:" + _format_ago_sec_only(aprs_ago_sec)
            line2 = "TU:" + _format_ago_sec_only(traccar_ago_sec)
            try:
                loc = utime.localtime()
                line3 = "%04d-%02d-%02d %02d:%02d:%02d" % (loc[0], loc[1], loc[2], loc[3], loc[4], loc[5])
            except Exception:
                line3 = (system_time_str or "--:--:--")
        else:
            line0 = "Acc/HDG/SAT"
            if accuracy_m is not None:
                try:
                    line1 = "ACC:" + "%3d" % min(999, max(0, int(round(float(accuracy_m)))))
                except (TypeError, ValueError):
                    line1 = "ACC: --"
            else:
                line1 = "ACC: --"
            if heading is not None:
                try:
                    line2 = "HDG:" + "%3d" % min(999, max(0, int(round(float(heading)))))
                except (TypeError, ValueError):
                    line2 = "HDG: --"
            else:
                line2 = "HDG: --"
            line3 = None

        if display_mode == 2:
            try:
                _sats_val = max(0, min(50, int(sats))) if sats is not None else 0
            except (TypeError, ValueError):
                _sats_val = 0
        else:
            _sats_val = 0

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
                _draw_content_line_incremental(PAGE_LINE3, sm["prev_line3"], line3, font_small)
                sm["prev_line3"] = line3
            sm["prev_sats"] = None
            sm["prev_bar_fill_w"] = None

        sm["oled_error_logged"] = False
        _flush()
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
        s = _state
        speed_str = "%03d" % min(999, max(0, int(round(float(speed_kmh or 0)))))
        bat_seg = round((bat_pct or 0) * BAT_SEGMENTS / 100) if bat_pct is not None else 0
        bat_seg = max(0, min(BAT_SEGMENTS, bat_seg))

        if not s["init_done"]:
            _draw_static_labels()
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

        if type_upd_line != (s.get("prev_type_upd") or ""):
            _fb_fill_rect_pages(_fb, COL_TYPE_UPD, COL_LEFT_MAX, PAGE_TYPE_UPD, PAGE_TYPE_UPD + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_TYPE_UPD, COL_TYPE_UPD, type_upd_line, font_small)
            s["prev_type_upd"] = type_upd_line

        if bat_pct is not None and bat_seg != s["prev_bat"]:
            _draw_battery(bat_seg)
            s["prev_bat"] = bat_seg

        s["oled_error_logged"] = False
        _flush()
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
            _draw_compact_static(title)
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
            _fb_fill_rect_pages(_fb, COL_C_LEFT, COL_C_LEFT_MAX, PAGE_C_TRACCAR, PAGE_C_TRACCAR + SMALL_H_PAGES - 1, 0x00)
            _draw_string(PAGE_C_TRACCAR, COL_C_LEFT, line_trcr, font_small)
            sc["prev_traccar_ago"] = line_trcr

        sc["oled_error_logged"] = False
        _flush()
    except Exception as e:
        if not sc.get("oled_error_logged"):
            _log.warning("oled_ssd1327 update_display_compact: %s" % e)
            sc["oled_error_logged"] = True
