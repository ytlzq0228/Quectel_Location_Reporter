# MicroPython 下用 TTF 在屏幕上高质量绘制任意高度字符

MicroPython 设备上**没有在运行时解析 TTF 的通用做法**（没有 Freetype/PIL），常见做法都是：**在 PC 上把 TTF 转成点阵字库，设备上只做“按字模画点”**。所谓“任意高度”= 转换时指定像素高度，生成对应字库；要多种高度就生成多份字库，设备上按需选用。

---

## 方案一：font-to-py + Writer（推荐，业界常用）

**[peterhinch/micropython-font-to-py](https://github.com/peterhinch/micropython-font-to-py)** 用 **Freetype** 在 PC 上把 TTF/OTF/BDF/PCF 转成 Python 字库，**指定像素高度**，质量很好；设备上用配套的 **Writer** 在 **framebuf** 上画字。

### 1. PC 上生成字库（指定高度，单位：像素）

```bash
pip install freetype-py   # 依赖
python font_to_py.py your_font.ttf 16 font_16.py   # 16 像素高
python font_to_py.py your_font.ttf 24 font_24.py   # 24 像素高
```

可选：`-f` 等宽、`-x` 水平映射等，见仓库说明。

### 2. 设备上：framebuf 驱动 + Writer

- 显示驱动必须是 **framebuf.FrameBuffer 子类**（如 `ssd1306.SSD1306_I2C`)，不能是裸 I2C 写寄存器。
- 把仓库里的 **writer** 目录（`writer.py` 等）和生成的 `font_xx.py` 拷到设备。
- 使用示例：

```python
import ssd1306
from machine import I2C, Pin
from writer import Writer
import font_16  # 或 font_24

i2c = I2C(0, scl=Pin(1), sda=Pin(0))
display = ssd1306.SSD1306_I2C(128, 64, i2c)
display.fill(0)
w = Writer(display, font_16)
Writer.set_textpos(display, 0, 0)
w.printstring("Hello\n")
display.show()
```

- **Writer** 通过 `device.blit(fbc, x, y)` 画每个字，支持换行、裁剪、多字体。

### 3. 和本项目的区别

本项目 **oled_display.py** 使用**裸 I2C + 寄存器**驱动 SSD1306（无 framebuf、无整屏缓冲），以便省内存、做增量更新。因此**不能直接接 Writer**（Writer 要求设备是 FrameBuffer）。若要用 font-to-py 的高质量字库，需要：

- 要么：改用 **ssd1306.SSD1306_I2C**（或同类 framebuf 驱动）+ Writer，整屏刷新；
- 要么：继续用本项目的 I2C 绘制逻辑，但用 **本项目的 ttf_to_oled_font.py** 按任意宽高生成字库（见方案二）。

---

## 方案二：本项目工具 ttf_to_oled_font.py + 可变尺寸字库

本项目提供的 **tools/ttf_to_oled_font.py** 用 **Pillow** 在 PC 上把 TTF 转成**任意宽×高**的点阵（如 5×7、5×9、8×16），生成 `.py` 或 `.bin`；**oled_display** 会按字库里的 WIDTH/HEIGHT 自动用对应尺寸绘制（若加载到外部字库）。

```bash
# 5×9
python3 tools/ttf_to_oled_font.py tools/PixelOperator.ttf -o Fonts/font_5x9.py -W 5 -H 9

# 8×16
python3 tools/ttf_to_oled_font.py tools/PixelOperator.ttf -o Fonts/font_8x16.py -W 8 -H 16
```

把生成的 `font_5x9.py`（或 `font_small.py`）放到设备 **/usr/Fonts/** 或与 **oled_display.py** 同目录；**oled_display** 会优先加载外部字库，并按字库中的 **WIDTH/HEIGHT** 计算每字占用的列/页并绘制，从而支持 5×7、5×9、6×8、8×16 等任意尺寸。

- **优点**：无需 framebuf、不增加整屏缓冲，和现有增量更新兼容；尺寸完全由你在转换时指定。
- **缺点**：每种尺寸要单独转一份字库；渲染用的是 Pillow 缩放，小字号下可能略逊于 Freetype（font-to-py）。
- **注意**：字高 > 8 像素时，一行会占 2 个 SSD1306 页（8 行为一页），当前紧凑布局按「每行 1 页」设计，若用 5×9 等会多页一行，可能出现行间重叠或超出 8 页；可减少显示行数或自行改布局常量。

---

## 小结

| 目标 | 做法 |
|------|------|
| 在 MicroPython 里“用 TTF 画任意高度” | 在 PC 上按**像素高度**把 TTF 转成点阵字库，设备上只画点阵。 |
| 高质量、多字号、愿意用 framebuf | 用 **font-to-py** 指定高度生成 .py，设备用 **Writer + SSD1306 framebuf 驱动**。 |
| 保持本项目裸 I2C、增量更新 | 用 **ttf_to_oled_font.py** 生成任意 **-W -H** 字库，放到设备后 **oled_display** 按字库尺寸绘制。 |

两种方案都是“**根据 TTF 在 PC 生成字库，设备按给定高度/尺寸绘制**”，不在设备上解析 TTF。

---

## 本项目已接入 font-to-py 字库

若在 **Fonts/** 或设备可访问路径下存在 **font_16.py**（及可选 **font_32.py**），且由 font-to-py 生成，**oled_display** 会优先用其绘制所有小字。加载顺序：先尝试打开 `Fonts/font_16.py`、`/usr/Fonts/font_16.py`、`font_16.py` 并 exec；若失败再尝试 `from Fonts import font_16` 或 `import font_16`。大号速度数字仍使用内置 12×24；**font_32** 已预留（`FONT_TO_PY_LARGE`），如需用 32 像素高字库画速度可自行改 `_draw_large_number` 并调整布局。
