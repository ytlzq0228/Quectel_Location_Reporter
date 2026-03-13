# test_oled.py - OLED 三款界面 + PowerKey 测试
#
# 调用 oled_display.update_display，模拟动态数据；短按 PowerKey 切换界面(0/1/2)，
# 长按>=3秒退出。无 PowerKey 时可用 Ctrl+C 退出。
# 运行：在设备上执行 test_oled.py

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import utime

try:
    import oled_display
except Exception as e:
    print("oled_display import failed:", e)
    oled_display = None

try:
    from misc import PowerKey, Power
    _has_powerkey = True
except Exception:
    PowerKey = None
    Power = None
    _has_powerkey = False

# PowerKey：长按>=3秒退出，短按切换显示界面
_powerkey_exit_requested = False
_powerkey_press_ts = None
_display_mode = 0  # 0=GNSS INFO, 1=Report Status, 2=Acc/HDG/SAT
LONG_PRESS_MS = 3000
SHORT_PRESS_MIN_MS = 50


def _powerkey_callback(status):
    """PowerKey 回调：status 0=松开，1=按下。长按>=3s 退出，短按切换显示模式。"""
    global _powerkey_exit_requested, _powerkey_press_ts, _display_mode
    if status == 1:
        _powerkey_press_ts = utime.ticks_ms()
    elif status == 0 and _powerkey_press_ts is not None:
        duration = utime.ticks_diff(utime.ticks_ms(), _powerkey_press_ts)
        if duration >= LONG_PRESS_MS:
            _powerkey_exit_requested = True
        elif duration >= SHORT_PRESS_MIN_MS:
            _display_mode = (_display_mode + 1) % 3
        _powerkey_press_ts = None


def main():
    global _powerkey_exit_requested, _display_mode
    _powerkey_exit_requested = False
    _display_mode = 0

    if oled_display is None:
        print("Cannot run: oled_display not available")
        return

    i2c = oled_display.init_oled()
    if i2c is None:
        print("OLED init failed (I2C/SSD1306 not found?)")
        return

    oled_display.show_boot_message(i2c, "OLED Test...")
    utime.sleep_ms(800)
    oled_display.clear(i2c)

    if _has_powerkey:
        try:
            pk = PowerKey()
            if pk.powerKeyEventRegister(_powerkey_callback) == 0:
                print("PowerKey: short=switch display, long>=3s=exit. Ctrl+C also exit.")
            else:
                print("PowerKey register failed. Use Ctrl+C to exit.")
        except Exception as e:
            print("PowerKey init error:", e)
    else:
        print("No PowerKey. Display cycles every 5s. Ctrl+C to exit.")

    t = 0
    try:
        while True:
            if _powerkey_exit_requested:
                print("PowerKey long press, exit.")
                break

            # 动态模拟数据
            speed_kmh = (t * 3) % 121
            bat_pct = (t * 5) % 101
            lat_disp = "31.%06d" % ((t * 12345) % 1000000)
            lon_disp = "121.%06d" % ((t * 67890) % 1000000)
            gnss_type = "GNSS" if (t // 5) % 2 == 0 else "LBS"
            aprs_ago_sec = t % 66
            traccar_ago_sec = (t * 2) % 91
            try:
                loc = utime.localtime()  # 无参：RTC 本地时间；utime.time() 为开机秒数非时间戳
                system_time_str = "%02d:%02d:%02d" % (loc[3], loc[4], loc[5])
            except Exception:
                system_time_str = "%02d:%02d:%02d" % ((t // 3600) % 24, (t // 60) % 60, t % 60)
            acc_vals = [1.5, 3.0, 5.2, 8.1, 12.0, 6.5, 2.1, 15.3, 25.0, 4.0]
            accuracy_m = acc_vals[t % len(acc_vals)]
            heading = (t * 37) % 361
            sats = 6 + (t % 7)

            oled_display.update_display(
                i2c,
                _display_mode,
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

            t += 1
            utime.sleep_ms(1000)
    except KeyboardInterrupt:
        print("Ctrl+C")
    finally:
        oled_display.clear(i2c)
        print("OLED cleared. Bye.")
        if _powerkey_exit_requested and _has_powerkey:
            try:
                Power.powerDown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
