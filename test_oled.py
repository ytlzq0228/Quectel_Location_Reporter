# test_oled.py - OLED 紧凑布局单独测试
# 调用 oled_display.update_display_compact，模拟标题、电量、速度、定位方式、
# 距上次上报 APRS/Traccar 时间、精度等字段的动态变化，便于调试显示效果。
# 运行：在设备上执行 test_oled.py，Ctrl+C 退出并黑屏。
import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import utime

try:
    import oled_display
except Exception as e:
    print("oled_display import failed:", e)
    oled_display = None


def main():
    if oled_display is None:
        print("Cannot run: oled_display not available")
        return
    i2c = oled_display.init_oled()
    if i2c is None:
        print("OLED init failed (I2C/SSD1306 not found?)")
        return
    # 每次运行都重置紧凑布局状态，避免上次 clear() 后状态仍为“已初始化”导致不重绘标题
    oled_display.reset_display_compact()
    print("OLED init OK. Display compact layout. Ctrl+C to exit.")

    t = 0
    try:
        while True:
            # 标题固定
            title = "Quec GNSS"

            # 电量 0~100 循环变化（每 2 秒约变 10%）
            bat_pct = (t * 5) % 101

            # 速度 0~120 km/h 循环
            speed_kmh = (t * 3) % 121

            # 定位方式在 GNSS / LBS 之间切换（每 5 秒）
            gnss_type = "GNSS" if (t // 5) % 2 == 0 else "LBS"

            # 距上次上报 APRS：每秒加 1，到 65 秒后归零
            aprs_ago_sec = t % 66

            # 距上次上报 Traccar：每 2 秒加 1，到 90 秒后归零
            traccar_ago_sec = (t * 2) % 91

            # 精度在 1.5 ~ 25.0 m 之间变化
            acc_values = [1.5, 3.0, 5.2, 8.1, 12.0, 6.5, 2.1, 15.3, 25.0, 4.0]
            accuracy_m = acc_values[t % len(acc_values)]

            oled_display.update_display_compact(
                i2c,
                title=title,
                bat_pct=bat_pct,
                speed_kmh=speed_kmh,
                gnss_type=gnss_type,
                aprs_ago_sec=aprs_ago_sec,
                traccar_ago_sec=traccar_ago_sec,
                accuracy_m=accuracy_m,
            )
            t += 1
            utime.sleep_ms(1000)
    except KeyboardInterrupt:
        pass
    finally:
        oled_display.clear(i2c)
        print("OLED cleared. Bye.")


if __name__ == "__main__":
    main()
