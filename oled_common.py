# oled_common.py - OLED 字库、字模转换、格式化与通用工具（SSD1306 / SSD1327 共用）


def wrap_font(g):
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


def load_font_py(paths, mod_name):
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
    return wrap_font(raw)


font_12 = load_font_py(
    ("Fonts/PixelOperator_12.py", "/usr/Fonts/PixelOperator_12.py", "PixelOperator_12.py"),
    "PixelOperator_12",
)
if font_12 is None:
    font_12 = load_font_py(("Fonts/font_12.py", "/usr/Fonts/font_12.py", "font_12.py"), "font_12")
font_32 = load_font_py(
    ("Fonts/PixelOperator_32.py", "/usr/Fonts/PixelOperator_32.py", "PixelOperator_32.py"),
    "PixelOperator_32",
)
if font_32 is None:
    font_32 = load_font_py(("Fonts/font_32.py", "/usr/Fonts/font_32.py", "font_32.py"), "font_32")

if font_12 is None:
    raise ImportError("oled_common requires 12px font (e.g. Fonts/PixelOperator_12.py).")
if font_32 is None:
    raise ImportError("oled_common requires 32px font (e.g. Fonts/PixelOperator_32.py).")

font_small = font_12
font_large = font_32

SMALL_H_PAGES = (font_small.height() + 7) // 8


def glyph_to_column_major(glyph_bytes, width, height):
    """font_to_py 行主序 MONO_HMSB → 列×页缓冲（与 SSD1306 GDDRAM 列顺序一致）。每列 1 空列 + width 列。"""
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
                        out_byte |= 1 << row
            buf[p * cols + 1 + c] = out_byte
    return buf


def measure_number_cols(s, font):
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


def first_last_diff(old_str, new_str):
    """返回 (first_diff, last_diff) 即首个和最后一个不同字符的下标（含）。"""
    old = old_str or ""
    new = new_str or ""
    first = len(old)
    for i in range(min(len(old), len(new))):
        if old[i] != new[i]:
            first = i
            break
    else:
        first = min(len(old), len(new))
    last = first - 1
    for i in range(max(len(old), len(new)) - 1, first - 1, -1):
        o = old[i] if i < len(old) else ""
        n = new[i] if i < len(new) else ""
        if o != n:
            last = i
            break
    return first, last


def format_ago(sec):
    if sec is None or sec < 0:
        return "--"
    s = int(sec)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def format_ago_sec_only(sec):
    if sec is None or sec < 0:
        return "---"
    s = min(999, max(0, int(sec)))
    return "%03d" % s


def format_lat_3d4_ns(s):
    if not s:
        return "---.----N"
    try:
        v = float(str(s).replace("N", "").replace("n", "").replace("S", "").replace("s", "").strip())
    except (TypeError, ValueError):
        return "---.----N"
    letter = "S" if v < 0 else "N"
    v = abs(v)
    i = int(v)
    f = round((v - i) * 10000)
    if f >= 10000:
        f = 0
        i += 1
    return "%03d.%04d%s" % (i, f, letter)


def format_lon_3d4_ew(s):
    if not s:
        return "---.----E"
    try:
        v = float(str(s).replace("E", "").replace("e", "").replace("W", "").replace("w", "").strip())
    except (TypeError, ValueError):
        return "---.----E"
    letter = "W" if v < 0 else "E"
    v = abs(v)
    i = int(v)
    f = round((v - i) * 10000)
    if f >= 10000:
        f = 0
        i += 1
    return "%03d.%04d%s" % (i, f, letter)
