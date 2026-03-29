# oled_display.py - OLED 统一入口：按 config.cfg 选择 SSD1306(I2C) 或 SSD1327(SPI)
#
# 对外接口保持不变：init_oled, clear, set_brightness, show_boot_message, update_menu_cursor,
# update_display, update_position, reset_display_compact, update_display_compact
#
# init_oled 返回句柄：("ssd1306", i2c) 或 ("ssd1327", True)；失败返回 None。
# 主程序可把返回值当作原 i2c 变量传入各函数，无需改调用方式。
import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import log


_log = log.getLogger("OLED")

_KIND_SSD1306 = "ssd1306"
_KIND_SSD1327 = "ssd1327"


def _read_oled_raw():
    try:
        import config as _cfg
        return _cfg.get_all_raw()
    except Exception:
        return {}


def _oled_type_from_raw(raw):
    t = (raw.get("oled_type") or _KIND_SSD1306).strip().lower()
    if t in ("ssd1327", "1327", "spi"):
        return _KIND_SSD1327
    return _KIND_SSD1306


def _ssd1327_cfg_from_raw(raw):
    def _gi(key, default):
        try:
            v = raw.get(key)
            if v is None or str(v).strip() == "":
                return default
            return int(str(v).strip(), 0)
        except Exception:
            return default

    return {
        "spi_port": _gi("oled_spi_port", 0),
        "spi_mode": _gi("oled_spi_mode", 0),
        "spi_clk": _gi("oled_spi_clk", 4),
        "spi_group": _gi("oled_spi_group", 1),
        "gpio_rst": _gi("oled_gpio_rst", 5),
        "gpio_dc": _gi("oled_gpio_dc", 7),
        "gpio_cs": _gi("oled_gpio_cs", 8),
        "remap": _gi("oled_ssd1327_remap", 0x51),
        "function_sel_a": _gi("oled_function_sel_a", 0x00),
        # SSD1327 外部 VPP 升压使能（高=开），逻辑供电独立；-1 表示不控制
        "boost_gpio": _gi("oled_boost_gpio", 20),
    }


def _unpack(handle):
    if handle is None:
        return None, None
    if isinstance(handle, tuple) and len(handle) == 2:
        return handle[0], handle[1]
    # 兼容：旧代码若直接传入 i2c 对象（非元组）
    return _KIND_SSD1306, handle


def init_oled():
    raw = _read_oled_raw()
    kind = _oled_type_from_raw(raw)
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            ctx = oled_ssd1327.init_oled(_ssd1327_cfg_from_raw(raw))
            if ctx is None:
                _log.error("oled_display: SSD1327 init failed (check SPI/GPIO and oled_*.py on /usr)")
                return None
            _log.info("oled_display: SSD1327 SPI ok")
            return (_KIND_SSD1327, ctx)
        import oled_ssd1306
        i2c = oled_ssd1306.init_oled()
        if i2c is None:
            _log.error("oled_display: SSD1306 I2C init failed")
            return None
        _log.info("oled_display: SSD1306 I2C ok")
        return (_KIND_SSD1306, i2c)
    except Exception as e:
        _log.error("oled_display init_oled error: %s" % e)
        return None


def set_brightness(handle, percent):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.set_brightness(ctx, percent)
        else:
            import oled_ssd1306
            oled_ssd1306.set_brightness(ctx, percent)
    except Exception as e:
        _log.error("oled_display set_brightness error: %s" % e)


def clear(handle, fill=0x00):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.clear(ctx, fill)
        else:
            import oled_ssd1306
            oled_ssd1306.clear(ctx, fill)
    except Exception as e:
        _log.error("oled_display clear error: %s" % e)


def show_boot_message(handle, msg="Booting..."):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.show_boot_message(ctx, msg)
        else:
            import oled_ssd1306
            oled_ssd1306.show_boot_message(ctx, msg)
    except Exception as e:
        _log.error("oled_display show_boot_message error: %s" % e)


def update_menu_cursor(handle, prev_row, new_row):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.update_menu_cursor(ctx, prev_row, new_row)
        else:
            import oled_ssd1306
            oled_ssd1306.update_menu_cursor(ctx, prev_row, new_row)
    except Exception as e:
        _log.error("oled_display update_menu_cursor error: %s" % e)


def update_display(
    handle,
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
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.update_display(
                ctx,
                display_mode,
                speed_kmh,
                bat_pct=bat_pct,
                lat_disp=lat_disp,
                lon_disp=lon_disp,
                gnss_type=gnss_type,
                aprs_ago_sec=aprs_ago_sec,
                traccar_ago_sec=traccar_ago_sec,
                system_time_str=system_time_str,
                accuracy_m=accuracy_m,
                heading=heading,
                sats=sats,
            )
        else:
            import oled_ssd1306
            oled_ssd1306.update_display(
                ctx,
                display_mode,
                speed_kmh,
                bat_pct=bat_pct,
                lat_disp=lat_disp,
                lon_disp=lon_disp,
                gnss_type=gnss_type,
                aprs_ago_sec=aprs_ago_sec,
                traccar_ago_sec=traccar_ago_sec,
                system_time_str=system_time_str,
                accuracy_m=accuracy_m,
                heading=heading,
                sats=sats,
            )
    except Exception as e:
        _log.error("oled_display update_display error: %s" % e)


def update_position(handle, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=None):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.update_position(ctx, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=bat_pct)
        else:
            import oled_ssd1306
            oled_ssd1306.update_position(ctx, lat_disp, lon_disp, gnss_type, update_time, time_dif, speed_kmh, bat_pct=bat_pct)
    except Exception as e:
        _log.error("oled_display update_position error: %s" % e)


def reset_display_compact():
    try:
        raw = _read_oled_raw()
        kind = _oled_type_from_raw(raw)
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.reset_display_compact()
        else:
            import oled_ssd1306
            oled_ssd1306.reset_display_compact()
    except Exception as e:
        _log.error("oled_display reset_display_compact error: %s" % e)


def update_display_compact(
    handle,
    title="Quec GNSS",
    bat_pct=None,
    speed_kmh=None,
    gnss_type=None,
    aprs_ago_sec=None,
    traccar_ago_sec=None,
    accuracy_m=None,
):
    kind, ctx = _unpack(handle)
    if ctx is None:
        return
    try:
        if kind == _KIND_SSD1327:
            import oled_ssd1327
            oled_ssd1327.update_display_compact(
                ctx,
                title=title,
                bat_pct=bat_pct,
                speed_kmh=speed_kmh,
                gnss_type=gnss_type,
                aprs_ago_sec=aprs_ago_sec,
                traccar_ago_sec=traccar_ago_sec,
                accuracy_m=accuracy_m,
            )
        else:
            import oled_ssd1306
            oled_ssd1306.update_display_compact(
                ctx,
                title=title,
                bat_pct=bat_pct,
                speed_kmh=speed_kmh,
                gnss_type=gnss_type,
                aprs_ago_sec=aprs_ago_sec,
                traccar_ago_sec=traccar_ago_sec,
                accuracy_m=accuracy_m,
            )
    except Exception as e:
        _log.error("oled_display update_display_compact error: %s" % e)
