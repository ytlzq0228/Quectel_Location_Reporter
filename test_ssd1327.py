from machine import SPI, Pin
import utime

# SSD1327 + QuecPython machine.SPI 直驱测试脚本
# 已确认连线：
# GPIO5 -> pin49 : LCD_RST
# GPIO7 -> pin51 : LCD_SPI_DC
# GPIO8 -> pin52 : LCD_SPI_CS
# SPI0 group1:
#   MOSI -> pin50
#   CLK  -> pin53

# -----------------------------
# 固定硬件配置
# -----------------------------
GPIO_RST = Pin.GPIO5
GPIO_DC = Pin.GPIO7
GPIO_CS = Pin.GPIO8

SPI_PORT = 0
SPI_MODE = 0
SPI_CLK = 4
SPI_GROUP = 1

WIDTH = 128
HEIGHT = 128
COL_BYTES = WIDTH // 2
FRAME_BYTES = COL_BYTES * HEIGHT  # 8192 bytes, 4bpp
CHUNK = 256

# 动画 / 计数测试（可按需改）
DIGIT_SCALE = 3
DIGIT_GAP = 4
COUNTER_TOTAL_MS = 8000
COUNTER_TICK_MS = 0

DIAG_SQUARE = 12
DIAG_SPEED = 5
DIAG_TOTAL_MS = 12000
DIAG_FRAME_MS = 0

# 5x7 点阵数字，每行 5 字符 '0'/'1'，从左到右
_DIG_STR = (
    ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    ("01110", "10001", "00001", "00110", "01000", "10000", "01110"),
    ("01110", "10001", "00001", "00110", "00001", "10001", "01110"),
    ("10001", "10001", "10001", "01111", "00001", "00001", "00001"),
    ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    ("01110", "10001", "10000", "11110", "10001", "10001", "01110"),
    ("01110", "10001", "00001", "00010", "00100", "01000", "01000"),
    ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    ("01110", "10001", "10001", "01111", "00001", "10001", "01110"),
)

# 这版已经验证可用：
# - 列地址按 0x00..0x7F 发送
# - remap = 0x51
# - function selection A = 0x00（VCI=VDD=1.8V 外部供电）
REMAP = 0x51
FUNCTION_SEL_A = 0x00

spi = None
pin_dc = None
pin_cs = None
pin_rst = None


def spi_write(buf):
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
    raise Exception("spi.write failed ret={}".format(ret))


def write_cmd(cmd):
    pin_cs.write(0)
    pin_dc.write(0)
    spi_write(bytes([cmd & 0xFF]))
    pin_cs.write(1)


def write_cmd_data(cmd, data_bytes):
    # 参考微雪 demo：命令和参数都按 command 流发送，参数阶段仍保持 DC=0
    pin_cs.write(0)
    pin_dc.write(0)
    spi_write(bytes([cmd & 0xFF]))
    if data_bytes:
        n = len(data_bytes)
        off = 0
        while off < n:
            end = min(off + CHUNK, n)
            spi_write(data_bytes[off:end])
            off = end
    pin_cs.write(1)


def _set_window_cmds_in_current_cs():
    # 在同一个 CS 会话里连续发送窗口与写 RAM 命令，参数仍按 command 流发送（DC=0）
    pin_dc.write(0)
    spi_write(b"\x15\x00\x7F")
    spi_write(b"\x75\x00\x7F")
    spi_write(b"\x5C")
    pin_dc.write(1)


def write_framebuffer(fb):
    if len(fb) != FRAME_BYTES:
        raise Exception("frame size invalid: {} != {}".format(len(fb), FRAME_BYTES))

    pin_cs.write(0)
    _set_window_cmds_in_current_cs()

    n = len(fb)
    off = 0
    while off < n:
        end = min(off + CHUNK, n)
        spi_write(fb[off:end])
        off = end

    pin_cs.write(1)


def reset_panel():
    pin_rst.write(1)
    utime.sleep_ms(10)
    pin_rst.write(0)
    utime.sleep_ms(30)
    pin_rst.write(1)
    utime.sleep_ms(120)


def gray_byte(gray4):
    g = gray4 & 0x0F
    return (g << 4) | g


def fill_gray(gray4):
    v = gray_byte(gray4)
    fb = bytearray(FRAME_BYTES)
    for i in range(FRAME_BYTES):
        fb[i] = v
    write_framebuffer(fb)


def build_vertical_gray_ramp():
    # 16 级灰度竖条（从左到右 0..15）
    fb = bytearray(FRAME_BYTES)
    bar_w = WIDTH // 16
    for y in range(HEIGHT):
        row_off = y * COL_BYTES
        for x in range(WIDTH):
            gray = x // bar_w
            if gray > 15:
                gray = 15
            idx = row_off + (x // 2)
            if (x & 1) == 0:
                fb[idx] = (gray << 4) | (fb[idx] & 0x0F)
            else:
                fb[idx] = (fb[idx] & 0xF0) | gray
    return fb


def fb_clear(fb, gray4=0):
    v = gray_byte(gray4 & 0x0F)
    for i in range(FRAME_BYTES):
        fb[i] = v


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


def draw_digit_glyph(fb, ox, oy, digit, scale, fg_gray4):
    glyph = _DIG_STR[digit % 10]
    fg = fg_gray4 & 0x0F
    for r in range(7):
        row = glyph[r]
        for c in range(5):
            if row[c] != "1":
                continue
            yb = oy + r * scale
            xb = ox + c * scale
            for sy in range(scale):
                yy = yb + sy
                for sx in range(scale):
                    fb_put_pixel(fb, xb + sx, yy, fg)


def draw_4digits_centered(fb, n, scale, gap, fg_gray4, bg_gray4):
    fb_clear(fb, bg_gray4)
    gw = 5 * scale
    gh = 7 * scale
    total_w = 4 * gw + 3 * gap
    ox0 = (WIDTH - total_w) // 2
    oy0 = (HEIGHT - gh) // 2
    n = n % 10000
    d0 = n // 1000
    d1 = (n // 100) % 10
    d2 = (n // 10) % 10
    d3 = n % 10
    step = gw + gap
    draw_digit_glyph(fb, ox0 + 0 * step, oy0, d0, scale, fg_gray4)
    draw_digit_glyph(fb, ox0 + 1 * step, oy0, d1, scale, fg_gray4)
    draw_digit_glyph(fb, ox0 + 2 * step, oy0, d2, scale, fg_gray4)
    draw_digit_glyph(fb, ox0 + 3 * step, oy0, d3, scale, fg_gray4)


def test_counter_0000_9999():
    print("PATTERN 6: 0000-9999 fast counter")
    write_cmd(0xA4)
    fb = bytearray(FRAME_BYTES)
    t0 = utime.ticks_ms()
    n = 0
    while utime.ticks_diff(utime.ticks_ms(), t0) < COUNTER_TOTAL_MS:
        draw_4digits_centered(fb, n, DIGIT_SCALE, DIGIT_GAP, 0xF, 0x0)
        write_framebuffer(fb)
        n = (n + 1) % 10000
        if COUNTER_TICK_MS:
            utime.sleep_ms(COUNTER_TICK_MS)


def test_diagonal_square():
    print("PATTERN 7: diagonal bouncing square")
    write_cmd(0xA4)
    fb = bytearray(FRAME_BYTES)
    sq = DIAG_SQUARE
    x, y = 0, 0
    vx, vy = DIAG_SPEED, DIAG_SPEED
    bg, fg = 0x0, 0xF
    t0 = utime.ticks_ms()
    while utime.ticks_diff(utime.ticks_ms(), t0) < DIAG_TOTAL_MS:
        fb_clear(fb, bg)
        fb_fill_rect(fb, x, y, sq, sq, fg)
        write_framebuffer(fb)
        nx = x + vx
        ny = y + vy
        if nx < 0:
            nx = 0
            vx = -vx
        elif nx + sq > WIDTH:
            nx = WIDTH - sq
            vx = -vx
        if ny < 0:
            ny = 0
            vy = -vy
        elif ny + sq > HEIGHT:
            ny = HEIGHT - sq
            vy = -vy
        x, y = nx, ny
        if DIAG_FRAME_MS:
            utime.sleep_ms(DIAG_FRAME_MS)


def init_ssd1327():
    reset_panel()

    write_cmd_data(0xFD, b"\x12")  # unlock
    write_cmd(0xAE)                  # display off

    write_cmd_data(0x15, b"\x00\x7F")  # column address
    write_cmd_data(0x75, b"\x00\x7F")  # row address

    write_cmd_data(0x81, b"\x80")       # contrast
    write_cmd_data(0xA0, bytes([REMAP & 0xFF]))
    write_cmd_data(0xA1, b"\x00")       # start line
    write_cmd_data(0xA2, b"\x00")       # display offset
    write_cmd(0xA4)                       # resume RAM display
    write_cmd(0xA6)                       # normal display (not inverse)
    write_cmd_data(0xA8, b"\x7F")       # mux ratio
    write_cmd_data(0xB1, b"\xF1")       # phase length
    write_cmd_data(0xB3, b"\x00")       # display clock
    write_cmd_data(0xAB, bytes([FUNCTION_SEL_A & 0xFF]))
    write_cmd(0xB9)                       # default linear grayscale table
    write_cmd_data(0xB6, b"\x0F")       # second pre-charge period
    write_cmd_data(0xBE, b"\x0F")       # VCOMH
    write_cmd_data(0xBC, b"\x08")       # pre-charge voltage
    write_cmd_data(0xD5, b"\x62")       # second pre-charge enable

    utime.sleep_ms(120)
    write_cmd(0xAF)                       # display on
    utime.sleep_ms(50)


def show_patterns():
    print("PATTERN 1: all pixels ON (A5)")
    write_cmd(0xA5)
    utime.sleep_ms(1000)

    # PATTERN1 结束后仍为 0xA5（整屏点亮）。若先写 GRAM 再 A4，部分屏在 A5 下写显存会花屏；
    # 先 0xA4 退出整屏点亮，再填满黑，最后再 A4 确保按 RAM 显示。
    print("PATTERN 2: A4 exit A5, write black RAM, then A4")
    write_cmd(0xA4)
    utime.sleep_ms(10)
    fill_gray(0x0)
    utime.sleep_ms(300)
    write_cmd(0xA4)
    utime.sleep_ms(1200)

    print("PATTERN 3: write white RAM, then A4")
    fill_gray(0xF)
    utime.sleep_ms(300)
    write_cmd(0xA4)
    utime.sleep_ms(1200)

    print("PATTERN 4: write 50% gray RAM, then A4")
    fill_gray(0x8)
    utime.sleep_ms(300)
    write_cmd(0xA4)
    utime.sleep_ms(1200)

    print("PATTERN 5: write 16-level gray ramp, then A4")
    write_framebuffer(build_vertical_gray_ramp())
    utime.sleep_ms(300)
    write_cmd(0xA4)
    utime.sleep_ms(2000)

    test_counter_0000_9999()
    test_diagonal_square()


def main():
    global spi, pin_dc, pin_cs, pin_rst

    print("SSD1327 direct SPI test start")
    print("SPI({}, {}, {}, group={}) DC={} CS={} RST={}".format(
        SPI_PORT, SPI_MODE, SPI_CLK, SPI_GROUP, GPIO_DC, GPIO_CS, GPIO_RST
    ))
    print("CFG: remap=0x{:02X} funcA=0x{:02X}".format(REMAP, FUNCTION_SEL_A))

    # 关键：先初始化 SPI，再重新把 DC/CS/RST 配成普通 GPIO。
    # 否则 SPI 初始化可能覆盖 GPIO7 的复用状态，导致 DC 不翻转。
    spi = SPI(SPI_PORT, SPI_MODE, SPI_CLK, SPI_GROUP)
    pin_rst = Pin(GPIO_RST, Pin.OUT, Pin.PULL_DISABLE, 1)
    pin_dc = Pin(GPIO_DC, Pin.OUT, Pin.PULL_DISABLE, 0)
    pin_cs = Pin(GPIO_CS, Pin.OUT, Pin.PULL_DISABLE, 1)

    try:
        init_ssd1327()
        show_patterns()
        print("TEST DONE")
    except Exception as e:
        print("TEST ERROR:", e)
    finally:
        try:
            write_cmd(0xAE)
        except Exception:
            pass
        try:
            spi.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()