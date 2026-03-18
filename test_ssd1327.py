# test_ssd1327.py - SSD1327 屏幕点亮测试（独立程序）
#
# 仅依赖 machine.I2C、utime，不依赖 oled_display 或字库。
# 用于验证 SSD1327 128x128 灰阶 OLED 能否正常点亮。
# 运行：在设备上执行 test_ssd1327.py
#
# 接线：SDA/SCL 与项目 SSD1306 相同（I2C0）；部分模块 SSD1327 I2C 地址为 0x3D。
# 若为 128x96 屏，将 HEIGHT=96，并在 _ssd1327_init 中 0x75 改为 0x00,0x5F、0xA8 改为 0x5F。

import utime
from machine import I2C

# 为 True 时执行测试 1~12；为 False 时仅执行 13、14
RUN_BASIC_TESTS = False

# -----------------------------------------------------------------------------
# 可调参数
# -----------------------------------------------------------------------------
I2C_PORT = I2C.I2C0
I2C_MODE = I2C.STANDARD_MODE
# SSD1327 常见 I2C 地址：0x3C 或 0x3D，若点不亮可改为 0x3D 试
SSD1327_ADDR = 0x3C
WIDTH = 128
HEIGHT = 128
# 每字节 2 像素（高 4bit + 低 4bit），列地址 0~63
COL_BYTES = WIDTH // 2

_CMD = bytearray([0x00])
_DATA = bytearray([0x40])
I2C_CHUNK = 32


def _cmd(i2c, *bytes_list):
    if not bytes_list:
        return
    b = bytearray(bytes_list)
    i2c.write(SSD1327_ADDR, _CMD, 1, b, len(b))


def _write_data(i2c, data):
    if not data:
        return
    n = len(data)
    off = 0
    while off < n:
        end = min(off + I2C_CHUNK, n)
        i2c.write(SSD1327_ADDR, _DATA, 1, data[off:end], end - off)
        off = end
        if off < n:
            utime.sleep_us(100)


def _set_window(i2c, col_start, col_end, row_start, row_end):
    """设置列/行地址窗口。列 0~63（每列 2 像素），行 0~127。"""
    _cmd(i2c, 0x15, col_start, col_end)
    _cmd(i2c, 0x75, row_start, row_end)


def _ssd1327_init(i2c):
    """SSD1327 初始化序列（128x128，I2C 与 SPI 命令相同）。"""
    _cmd(i2c, 0xAE)                     # Display Off
    _cmd(i2c, 0x15, 0x00, 0x3F)         # Set column address 0~63
    _cmd(i2c, 0x75, 0x00, 0x7F)         # Set row address 0~127
    _cmd(i2c, 0x81, 0x80)               # Contrast
    _cmd(i2c, 0xA0, 0x51)               # Re-map (column/COM)
    _cmd(i2c, 0xA1, 0x00)               # Start line
    _cmd(i2c, 0xA2, 0x00)               # Display offset
    _cmd(i2c, 0xA4)                     # Normal display
    _cmd(i2c, 0xA8, 0x7F)               # Multiplex ratio 128
    _cmd(i2c, 0xB1, 0xF1)               # Phase length
    _cmd(i2c, 0xB3, 0x00)               # DCLK
    _cmd(i2c, 0xAB, 0x01)               # Function selection A
    _cmd(i2c, 0xB6, 0x0F)               # Phase 2
    _cmd(i2c, 0xBE, 0x0F)               # VCOMH
    _cmd(i2c, 0xBC, 0x08)               # Pre-charge voltage
    _cmd(i2c, 0xD5, 0x62)               # Function B
    _cmd(i2c, 0xFD, 0x12)               # Unlock
    utime.sleep_ms(200)
    _cmd(i2c, 0xAF)                     # Display On


def _fill_grayscale(i2c, gray):
    """
    全屏填充同一灰阶。gray 0~15（0=黑，15=白）。
    每字节两像素：高 4bit 与低 4bit 均为 gray。
    """
    byte_val = (gray << 4) | gray
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    total = COL_BYTES * HEIGHT
    chunk = bytearray(min(I2C_CHUNK, total))
    for i in range(len(chunk)):
        chunk[i] = byte_val
    sent = 0
    while sent < total:
        n = min(len(chunk), total - sent)
        _write_data(i2c, chunk[:n])
        sent += n


def _fill_grayscale_ramp_h(i2c):
    """水平灰阶条：从左到右 16 列，每列灰阶 0~15 循环。"""
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    for row in range(HEIGHT):
        buf = bytearray(COL_BYTES)
        for c in range(COL_BYTES):
            g = (c // 4) % 16
            buf[c] = (g << 4) | g
        _write_data(i2c, buf)


def _fill_grayscale_ramp_v(i2c):
    """垂直灰阶条：从上到下 16 段，每段灰阶 0~15。"""
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    band = HEIGHT // 16
    for seg in range(16):
        gray = seg
        byte_val = (gray << 4) | gray
        for _ in range(band):
            _write_data(i2c, bytearray([byte_val] * COL_BYTES))
    rem = HEIGHT - band * 16
    if rem > 0:
        _write_data(i2c, bytearray([0x77] * COL_BYTES * rem))


def _fill_checkerboard(i2c, block=8):
    """棋盘格：block x block 像素一块，交替 0 与 15。"""
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    for row in range(HEIGHT):
        buf = bytearray(COL_BYTES)
        for c in range(COL_BYTES):
            x = c * 2
            g = 15 if ((row // block) + (x // block)) % 2 == 0 else 0
            buf[c] = (g << 4) | g
        _write_data(i2c, buf)


def _fill_h_stripes(i2c, stripe_h=4):
    """水平条纹：每 stripe_h 行一条，黑白相间。"""
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    white = (15 << 4) | 15
    black = 0
    for row in range(HEIGHT):
        val = white if (row // stripe_h) % 2 == 0 else black
        _write_data(i2c, bytearray([val] * COL_BYTES))


def _fill_v_stripes(i2c, stripe_w=8):
    """垂直条纹：每 stripe_w 像素宽一条，黑白相间。"""
    _set_window(i2c, 0, COL_BYTES - 1, 0, HEIGHT - 1)
    white = (15 << 4) | 15
    black = 0
    for row in range(HEIGHT):
        buf = bytearray(COL_BYTES)
        for c in range(COL_BYTES):
            x = c * 2
            buf[c] = white if (x // stripe_w) % 2 == 0 else black
        _write_data(i2c, buf)


def _fill_border(i2c, border=4, inner_gray=0, border_gray=15):
    """边框：四边 border 像素为 border_gray，中间为 inner_gray。"""
    _fill_grayscale(i2c, inner_gray)
    bi = (border_gray << 4) | border_gray
    # 上
    _set_window(i2c, 0, COL_BYTES - 1, 0, border - 1)
    for _ in range(border):
        _write_data(i2c, bytearray([bi] * COL_BYTES))
    # 下
    _set_window(i2c, 0, COL_BYTES - 1, HEIGHT - border, HEIGHT - 1)
    for _ in range(border):
        _write_data(i2c, bytearray([bi] * COL_BYTES))
    # 左：按列写，每列 2 像素宽
    col_left = border // 2
    _set_window(i2c, 0, col_left - 1, border, HEIGHT - border - 1)
    for _ in range(HEIGHT - 2 * border):
        _write_data(i2c, bytearray([bi] * col_left))
    # 右
    col_right_start = COL_BYTES - (border + 1) // 2
    _set_window(i2c, col_right_start, COL_BYTES - 1, border, HEIGHT - border - 1)
    for _ in range(HEIGHT - 2 * border):
        _write_data(i2c, bytearray([bi] * (COL_BYTES - col_right_start)))


def _set_contrast(i2c, value):
    """设置对比度，value 0~255。"""
    _cmd(i2c, 0x81, value & 0xFF)


def _fill_rect_pixels(i2c, px, py, w, h, gray):
    """在像素坐标 (px,py) 处填充 w x h 矩形，灰阶 gray 0~15。只更新该区域。"""
    if w <= 0 or h <= 0 or px + w > WIDTH or py + h > HEIGHT:
        return
    col_start = px // 2
    col_end = (px + w - 1) // 2
    row_start = py
    row_end = py + h - 1
    byte_val = (gray << 4) | gray
    ncols = col_end - col_start + 1
    _set_window(i2c, col_start, col_end, row_start, row_end)
    for _ in range(h):
        _write_data(i2c, bytearray([byte_val] * ncols))


# -----------------------------------------------------------------------------
# 测试页 1：20x20 斜向移动方块（差量更新，合并区域单次写入防闪烁）
# -----------------------------------------------------------------------------
def _write_union_rect(i2c, old_xy, new_xy, box_size):
    """
    将「旧方块区域」与「新方块区域」的并集一次性写入：新方块内为白(15)，其余为黑(0)。
    避免先擦后画导致的闪烁。
    """
    ox, oy = old_xy
    nx, ny = new_xy
    ux = min(ox, nx) if ox >= 0 else nx
    uy = min(oy, ny) if oy >= 0 else ny
    ux2 = max(ox + box_size, nx + box_size) if ox >= 0 else nx + box_size
    uy2 = max(oy + box_size, ny + box_size) if oy >= 0 else ny + box_size
    uw = ux2 - ux
    uh = uy2 - uy
    col_start = ux // 2
    col_end = (ux + uw - 1) // 2
    ncols = col_end - col_start + 1
    _set_window(i2c, col_start, col_end, uy, uy2 - 1)
    for row in range(uh):
        py = uy + row
        buf = bytearray(ncols)
        for b in range(ncols):
            px_lo = ux + b * 2
            px_hi = ux + b * 2 + 1
            v_lo = 15 if (nx <= px_lo < nx + box_size and ny <= py < ny + box_size) else 0
            v_hi = 15 if (nx <= px_hi < nx + box_size and ny <= py < ny + box_size) else 0
            buf[b] = (v_lo << 4) | v_hi
        _write_data(i2c, buf)


def test_moving_square(i2c, duration_ms=8000, step=3, box_size=20):
    """
    黑底上 20x20 白块斜向弹跳，每帧只更新「旧+新」并集区域并一次性写入，减少闪烁。
    """
    _fill_grayscale(i2c, 0)
    x = 0
    y = 0
    dx = step
    dy = step
    prev_x = -1
    prev_y = -1
    t0 = utime.ticks_ms()
    while utime.ticks_diff(utime.ticks_ms(), t0) < duration_ms:
        _write_union_rect(i2c, (prev_x, prev_y), (x, y), box_size)
        prev_x = x
        prev_y = y
        x += dx
        y += dy
        if x + box_size >= WIDTH or x <= 0:
            dx = -dx
            x = max(0, min(WIDTH - box_size, x))
        if y + box_size >= HEIGHT or y <= 0:
            dy = -dy
            y = max(0, min(HEIGHT - box_size, y))
        utime.sleep_ms(25)


# -----------------------------------------------------------------------------
# 测试页 2：0000~9999 快速计数（Fonts 12px 字体，只更新变化位）
# -----------------------------------------------------------------------------
def _wrap_font(g):
    """将 exec 得到的字库 dict 封装成 get_ch / height / max_width 接口。"""
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


def _load_font_12():
    """加载 Fonts 目录下 12 号字体（与 oled_display 方案一致）。"""
    raw = None
    for mod_name in ("PixelOperator_12", "font_12"):
        try:
            pkg = __import__("Fonts", fromlist=[mod_name])
            raw = getattr(pkg, mod_name, None)
            if raw is not None and hasattr(raw, "get_ch"):
                break
        except Exception:
            pass
    if raw is None:
        for path in ("Fonts/PixelOperator_12.py", "/usr/Fonts/PixelOperator_12.py", "PixelOperator_12.py",
                     "Fonts/font_12.py", "/usr/Fonts/font_12.py", "font_12.py"):
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


def _glyph_to_ssd1327(glyph_bytes, width, height):
    """将 1bpp 字模（行主序 MONO_HMSB）转为 SSD1327：每字节 2 像素，亮=15 暗=0。"""
    if hasattr(glyph_bytes, "__getitem__"):
        g = glyph_bytes
    else:
        g = bytes(glyph_bytes)
    bpr = (width + 7) // 8
    ncol_bytes = (width + 1) // 2
    out = bytearray(ncol_bytes * height)
    for row in range(height):
        for b in range(ncol_bytes):
            px_lo = b * 2
            px_hi = b * 2 + 1
            bit_lo = (g[row * bpr + px_lo // 8] >> (7 - px_lo % 8)) & 1 if px_lo < width else 0
            bit_hi = (g[row * bpr + px_hi // 8] >> (7 - px_hi % 8)) & 1 if px_hi < width else 0
            out[row * ncol_bytes + b] = (15 * bit_lo << 4) | (15 * bit_hi)
    return out


def _draw_char_ssd1327(i2c, font, ch, px, py, clear_cell=True):
    """在像素 (px, py) 画一个字符，使用 12px 字体。若 clear_cell 则先清空字宽+1 列。返回字宽+1。"""
    try:
        glyph, h, w = font.get_ch(ch)
    except Exception:
        try:
            glyph, h, w = font.get_ch("?")
        except Exception:
            return font.max_width() + 1
    cell_w = font.max_width() + 1
    col_start = px // 2
    col_end = (px + cell_w - 1) // 2
    row_start = py
    row_end = py + h - 1
    ncols = col_end - col_start + 1
    ncol_bytes = (w + 1) // 2
    if clear_cell:
        _set_window(i2c, col_start, col_end, row_start, row_end)
        for _ in range(h):
            _write_data(i2c, bytearray([0] * ncols))
    buf = _glyph_to_ssd1327(glyph, w, h)
    _set_window(i2c, col_start, col_start + ncol_bytes - 1, row_start, row_end)
    for row in range(h):
        _write_data(i2c, buf[row * ncol_bytes:(row + 1) * ncol_bytes])
    return cell_w


def test_counter_0000_9999(i2c, duration_ms=10000, interval_ms=15):
    """
    黑底中显示 0000~9999 循环计数，使用 Fonts 目录 12px 字体，只重绘发生变化的数字位。
    """
    font = _load_font_12()
    if font is None:
        print("  Font 12 not found, skip counter test.")
        return
    fh = font.height()
    cell_w = font.max_width() + 1
    total_w = 4 * cell_w
    start_x = (WIDTH - total_w) // 2
    start_y = (HEIGHT - fh) // 2
    _fill_grayscale(i2c, 0)
    prev_digits = [-1, -1, -1, -1]
    t0 = utime.ticks_ms()
    n = 0
    while utime.ticks_diff(utime.ticks_ms(), t0) < duration_ms:
        d0 = n // 1000
        d1 = (n // 100) % 10
        d2 = (n // 10) % 10
        d3 = n % 10
        digits = (d0, d1, d2, d3)
        for i in range(4):
            if digits[i] != prev_digits[i]:
                _draw_char_ssd1327(i2c, font, str(digits[i]), start_x + i * cell_w, start_y, clear_cell=True)
        prev_digits = list(digits)
        n = (n + 1) % 10000
        utime.sleep_ms(interval_ms)


def _run(i2c, name, fn, delay_ms=2000):
    try:
        print(name)
        fn(i2c)
        utime.sleep_ms(delay_ms)
    except Exception as e:
        print("  Error: %s" % e)


def main():
    print("SSD1327 test: init I2C...")
    try:
        i2c = I2C(I2C_PORT, I2C_MODE)
        utime.sleep_ms(50)
    except Exception as e:
        print("I2C init error: %s" % e)
        return

    print("SSD1327 init...")
    try:
        _ssd1327_init(i2c)
    except Exception as e:
        print("SSD1327 init error (addr=0x%02X?): %s" % (SSD1327_ADDR, e))
        return

    _cmd(i2c, 0xA4)

    if RUN_BASIC_TESTS:
        # 1) 全亮命令
        print("[1] All pixels ON (0xA5)...")
        _cmd(i2c, 0xA5)
        utime.sleep_ms(2000)
        _cmd(i2c, 0xA4)

        # 2) 全黑
        _run(i2c, "[2] Full black (gray 0)...", lambda i: _fill_grayscale(i, 0))

        # 3) 全白
        _run(i2c, "[3] Full white (gray 15)...", lambda i: _fill_grayscale(i, 15))

        # 4) 灰阶 50%
        _run(i2c, "[4] Mid gray (gray 8)...", lambda i: _fill_grayscale(i, 8))

        # 5) 水平灰阶条
        _run(i2c, "[5] Horizontal grayscale ramp...", _fill_grayscale_ramp_h)

        # 6) 垂直灰阶条
        _run(i2c, "[6] Vertical grayscale ramp...", _fill_grayscale_ramp_v)

        # 7) 棋盘格
        _run(i2c, "[7] Checkerboard...", _fill_checkerboard)

        # 8) 水平条纹
        _run(i2c, "[8] Horizontal stripes...", _fill_h_stripes)

        # 9) 垂直条纹
        _run(i2c, "[9] Vertical stripes...", _fill_v_stripes)

        # 10) 白边黑底
        _run(i2c, "[10] Border (white frame)...", _fill_border)

        # 11) 对比度变化（全白背景下调对比度）
        print("[11] Contrast sweep (full white)...")
        _fill_grayscale(i2c, 15)
        for v in [255, 180, 120, 80, 40, 80, 120, 180, 255]:
            _set_contrast(i2c, v)
            utime.sleep_ms(400)
        _set_contrast(i2c, 0x80)

        # 12) 灰阶 0~15 逐级
        print("[12] Grayscale steps 0->15...")
        for g in range(16):
            _fill_grayscale(i2c, g)
            utime.sleep_ms(300)

    # 13) 移动方块（差量更新）
    print("[13] Moving square (delta update)...")
    test_moving_square(i2c, duration_ms=8000, step=3, box_size=20)

    # 14) 0000~9999 计数（只更新变化位）
    print("[14] Counter 0000-9999 (delta digits)...")
    test_counter_0000_9999(i2c, duration_ms=10000, interval_ms=15)

    print("Display OFF, exit.")
    try:
        _cmd(i2c, 0xAE)
    except Exception:
        pass
    print("SSD1327 test done.")


if __name__ == "__main__":
    main()
