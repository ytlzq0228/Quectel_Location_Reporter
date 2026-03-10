# GNSS_Reporter.py - 移远 EC800M QuecPython 定位上报主循环与逻辑
#
# 功能：优先 GNSS 定位；无 GNSS 时用 LBS 基站定位；按运动/静止策略打点并入队 Traccar/APRS。
#       Traccar、APRS 采用生产-消费异步上报（Queue + 文件持久化），主循环只负责按间隔记录点位，不阻塞网络。
#       配置从 config.cfg 读取，设备 ID 使用 IMEI；刷机引脚未悬空时退出。
#       LBS 定位时按静止间隔上报。APRS 在 aprs_report.py，Traccar 在 traccar_report.py。

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

import utime
import uos
import net
import modem
import quecgnss
import usocket as socket
import dataCall
import checkNet
from machine import Pin, WDT
from misc import PowerKey,Power

try:
    import battery
except Exception as e:
    print("battery import failed:", e)
    battery = None

try:
    import cell_info
except Exception as e:
    print("cell_info import failed:", e)
    cell_info = None

try:
    import cellLocator
except Exception as e:
    print("cellLocator import failed:", e)
    cellLocator = None

try:
    import aprs_report
except Exception as e:
    print("aprs_report import failed:", e)
    aprs_report = None

try:
    import traccar_report
except Exception as e:
    print("traccar_report import failed:", e)
    traccar_report = None

try:
    import config
except Exception as e:
    print("config import failed:", e)
    config = None

try:
    import oled_display
except Exception as e:
    print("oled_display import failed:", e)

    class _OledStub:
        """无 oled_display 模块时使用，仅保证调用不报错；init_oled 返回 None 表示无屏。"""
        def init_oled(self):
            return None
        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                pass
            return _noop
    oled_display = _OledStub()

# ------------------------- 默认常量 -------------------------
CID = 1
PROFILE = 0
FLASH_CHECK_INTERVAL_TICKS = 30


def load_config():
    """从 config 模块读取完整配置；若 config 不可用则返回默认 dict。"""
    if config:
        return config.load_config()
    return {
        "traccar_host": "traccar.example.com",
        "traccar_port": 5055,
        "moving_interval": 10,
        "still_interval": 300,
        "still_speed_threshold": 5,
        "flash_gpio": -1,
        "network_timeout": 60,
        "wdt_period": 60,
        "lbs_server": "",
        "lbs_port": 80,
        "lbs_token": "",
        "lbs_timeout": 30,
        "lbs_profile_idx": 1,
        "lbs_interval": 60,
    }


# ------------------------- 刷机引脚 -------------------------
def create_flash_pin(gpio_num):
    if gpio_num < 0:
        return None
    try:
        return Pin(gpio_num, Pin.IN, Pin.PULL_PU, 1)
    except Exception:
        return None


def is_flash_mode(pin):
    if pin is None:
        return False
    try:
        return pin.read() == 0
    except Exception:
        return False


# ------------------------- IMEI -------------------------
def get_device_id():
    try:
        imei = modem.getDevImei()
        if imei and imei != -1:
            return str(imei)
    except Exception as e:
        print("getDevImei error:", e)
    return "EC800M"


# ------------------------- GNSS 解析 -------------------------
def safe_decode(b):
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return str(b)


def dm_to_deg(dm, hemi):
    if not dm:
        return None
    try:
        v = float(dm)
    except Exception:
        return None
    d = int(v // 100)
    m = v - d * 100
    deg = d + m / 60.0
    if hemi in ("S", "W"):
        deg = -deg
    return deg


def parse_gga(line):
    f = line.split(",")
    if len(f) < 10:
        return None
    fix = f[6] or "0"
    sats = f[7] or "0"
    hdop = f[8] or ""
    alt = f[9] or ""
    return fix, sats, hdop, alt


def parse_rmc(line):
    f = line.split(",")
    if len(f) < 10:
        return None
    status = f[2] or "V"
    lat = dm_to_deg(f[3], f[4])
    lon = dm_to_deg(f[5], f[6])
    spd_kn = f[7] or "0"
    course = f[8] or ""
    date = f[9] or ""
    time_utc = f[1] or ""
    return status, lat, lon, spd_kn, course, date, time_utc


gps_data = {
    "lat": None,
    "lon": None,
    "speed": 0,
    "track": None,
    "alt": None,
    "sats": 0,
    "hdop": None,
    "fix": "0",
    "accuracy": None,  # GNSS: 由 HDOP 推算(eph)；LBS: 接口返回米
}


def gnss_read_once():
    data = quecgnss.read(4096)
    if isinstance(data, (bytes, bytearray)):
        raw = data
    else:
        try:
            raw = data[1]
        except Exception:
            raw = b""
    if not raw:
        return
    text = safe_decode(raw)
    for line in text.split("\r\n"):
        if not line or not line.startswith("$"):
            continue
        if line.startswith("$GNGGA") or line.startswith("$GPGGA") or line.startswith("$BDGGA"):
            g = parse_gga(line)
            if g:
                fix, sats, hdop, alt = g
                gps_data["fix"] = fix
                gps_data["sats"] = int(sats) if sats else 0
                gps_data["hdop"] = hdop
                try:
                    gps_data["accuracy"] = float(hdop) * 2.5 if hdop else None
                except Exception:
                    gps_data["accuracy"] = None
                try:
                    gps_data["alt"] = float(alt) if alt else None
                except Exception:
                    gps_data["alt"] = None
        elif line.startswith("$GNRMC") or line.startswith("$GPRMC") or line.startswith("$BDRMC"):
            r = parse_rmc(line)
            if r:
                status, lat, lon, spd_kn, course, date, time_utc = r
                if status == "A" and lat is not None and lon is not None:
                    gps_data["lat"] = lat
                    gps_data["lon"] = lon
                    try:
                        gps_data["speed"] = float(spd_kn) * 1.852
                    except Exception:
                        gps_data["speed"] = 0
                    try:
                        gps_data["track"] = float(course) if course else None
                    except Exception:
                        gps_data["track"] = None


# ------------------------- LBS 基站定位（GNSS 无数据时备用）-------------------------
def get_lbs_location(cfg):
    """调用 cellLocator.getLocation，成功返回 (lat, lon, accuracy)，失败返回 (None, None, None)。"""
    if not cellLocator:
        return None, None, None
    server = cfg.get("lbs_server", "").strip()
    token = cfg.get("lbs_token", "").strip()
    if not server or not token or len(token) != 16:
        return None, None, None
    port = int(cfg.get("lbs_port", 80))
    timeout = int(cfg.get("lbs_timeout", 30))
    profile_idx = int(cfg.get("lbs_profile_idx", 1))
    try:
        result = cellLocator.getLocation(server, port, token, timeout, profile_idx)
    except Exception:
        return None, None, None
    if isinstance(result, tuple) and len(result) >= 3:
        lon, lat, accuracy = result[0], result[1], result[2]
        if (lon, lat, accuracy) != (0.0, 0.0, 0):
            return float(lat), float(lon), int(accuracy)
    return None, None, None


# ------------------------- 时间 -------------------------
def get_utc_timestamp():
    try:
        ts = utime.mktime(utime.localtime())
        return ts
    except Exception:
        return 0


# ------------------------- Traccar 载荷构造 -------------------------
def build_traccar_payload(device_id, lat, lon, gps_data):
    """根据 gps_data 构造 Traccar 单条位置 payload（id/lat/lon/timestamp 及可选 speed/bearing/altitude 等）。"""
    payload = {
        "id": device_id,
        "lat": "%.7f" % lat,
        "lon": "%.7f" % lon,
        "timestamp": get_utc_timestamp(),
    }
    speed = gps_data.get("speed")
    if speed is not None:
        payload["speed"] = "%.2f" % (float(speed) / 1.852)
    track = gps_data.get("track")
    if track is not None:
        payload["bearing"] = "%.1f" % float(track)
    alt = gps_data.get("alt")
    if alt is not None:
        payload["altitude"] = "%.1f" % float(alt)
    sats = gps_data.get("sats")
    if sats is not None:
        payload["sat"] = sats
    acc = gps_data.get("accuracy")
    if acc is not None:
        payload["accuracy"] = "%.1f" % float(acc)
    if battery:
        try:
            level, voltage = battery.get_battery()
            if level is not None:
                payload["batteryLevel"] = "%.1f" % level
            if voltage is not None:
                payload["batteryVoltage"] = voltage
        except Exception:
            pass
    if net:
        try:
            payload["rssi"] = net.csqQueryPoll()
        except Exception as e:
            print("net.csqQueryPoll error:", e)
    if cell_info:
        try:
            payload["cell"] = cell_info.get_cell_info()
        except Exception as e:
            print("cell_info.get_cell_info error:", e)
    return payload


# PowerKey 长按退出：按下超过 1 秒后松开则请求退出（主循环检测此标志）
_powerkey_exit_requested = False
_powerkey_press_ts = None


def _powerkey_callback(status):
    """PowerKey 回调：status 0=松开，1=按下。松开时若按下时长>=1s 则请求退出。"""
    global _powerkey_exit_requested, _powerkey_press_ts
    if status == 1:
        _powerkey_press_ts = utime.ticks_ms()
    elif status == 0 and _powerkey_press_ts is not None:
        if utime.ticks_diff(utime.ticks_ms(), _powerkey_press_ts) >= 1000:
            _powerkey_exit_requested = True
        _powerkey_press_ts = None


# ------------------------- 主流程 -------------------------
def main():
    global _powerkey_exit_requested
    _powerkey_exit_requested = False
    print("GNSS_Reporter starting...")

    # 第一时间初始化 OLED 并显示 Booting（无屏或异常时 oled_display 内部静默）
    oled_i2c = oled_display.init_oled()
    oled_display.show_boot_message(oled_i2c, "Booting...")

    def oled_status(msg):
        """将状态/报错同步到 OLED 单行（无上位机时便于看运行状态）。"""
        oled_display.show_boot_message(oled_i2c, str(msg)[:21])

    if oled_i2c is not None:
        print("OLED init ok")

    cfg = load_config()
    print("config:", cfg)

    flash_pin = create_flash_pin(cfg["flash_gpio"])
    if is_flash_mode(flash_pin):
        print("Flash pin asserted, exit for flash mode.")
        oled_status("Flash mode exit")
        raise SystemExit

    device_id = get_device_id()
    print("device_id:", device_id)
    oled_status("IMEI:****" + str(device_id)[-6:])

    print("wait network...")
    oled_status("wait network...")
    stagecode, subcode = checkNet.waitNetworkReady(cfg["network_timeout"])
    if stagecode != 3:
        print("network not ready, exit.")
        oled_status("net not ready")
        raise SystemExit
    print("network ready")
    oled_status("network ready")

    try:
        dataCall.getInfo(CID, PROFILE)
        utime.sleep(1)
    except Exception as e:
        print("dataCall.getInfo:", e)
        oled_status("dataCall err")

    try:
        quecgnss.configSet(0, 1)
        quecgnss.configSet(2, 1)
        quecgnss.configSet(4, 1)
    except Exception as e:
        print("quecgnss configSet:", e)
    ret = quecgnss.init()
    if ret != 0:
        print("GNSS init failed, ret:", ret)
        oled_status("GNSS init failed")
        raise SystemExit
    print("GNSS init ok")
    oled_status("GNSS init ok")

    moving_interval = cfg["moving_interval"]
    still_interval = cfg["still_interval"]
    still_speed_threshold = cfg["still_speed_threshold"]

    traccar_cfg = traccar_report.load_config() if traccar_report else {}
    if traccar_report and (traccar_cfg.get("traccar_host") or "").strip():
        traccar_report.start_consumer(traccar_cfg, device_id)

    aprs_cfg = aprs_report.load_config() if aprs_report else {}
    last_aprs_ts = 0
    if aprs_report and aprs_cfg.get("aprs_callsign"):
        aprs_report.start_consumer(aprs_cfg)

    wdt = None
    if cfg["wdt_period"] > 0:
        try:
            wdt = WDT(cfg["wdt_period"])
            print("WDT started, period %d s" % cfg["wdt_period"])
            oled_status("WDT %ds" % cfg["wdt_period"])
        except Exception as e:
            print("WDT init failed:", e)
            oled_status("WDT init fail")

    last_report_ts = 0
    last_still_report_ts = 0
    last_lbs_ts = 0
    tick = 0

    try:
        pk = PowerKey()
        if pk.powerKeyEventRegister(_powerkey_callback) == 0:
            print("PowerKey registered: long press >= 1s to exit.")
            oled_status("PowerKey Register ok")
        else:
            print("PowerKey register failed.")
            oled_status("PowerKey Register fail")
    except Exception as e:
        print("PowerKey init error:", e)
        oled_status("PowerKey Init err")
    oled_display.clear(oled_i2c)
    try:
        while True:
            try:
                now = utime.time()
            except Exception:
                now = 0
            if wdt:
                try:
                    wdt.feed()
                except Exception:
                    pass
            try:
                tick += 1
                if _powerkey_exit_requested:
                    print("PowerKey long press, exit.")
                    oled_status("PowerKey exit")
                    break
                if tick % FLASH_CHECK_INTERVAL_TICKS == 0 and is_flash_mode(flash_pin):
                    print("Flash pin asserted, exit.")
                    oled_status("Flash mode exit")
                    break

                # 1) 优先 GNSS；无有效 lat/lon 时按间隔尝试 LBS（LBS 按次计费，用 lbs_interval 限频）
                gnss_read_once()
                #print(gps_data)
                lat = gps_data.get("lat")
                lon = gps_data.get("lon")
                lbs_interval = cfg.get("lbs_interval", 60)
                if (lat is None or lon is None or gps_data.get("fix") == "0") and cfg.get("lbs_token") and cellLocator:
                    if (now - last_lbs_ts) >= lbs_interval:
                        lbs_lat, lbs_lon, lbs_acc = get_lbs_location(cfg)
                        last_lbs_ts = now
                        if lbs_lat is not None and lbs_lon is not None:
                            gps_data["lat"], gps_data["lon"] = lbs_lat, lbs_lon
                            gps_data["speed"] = 0
                            gps_data["accuracy"] = lbs_acc
                            gps_data["_source"] = "LBS"
                            lat, lon = lbs_lat, lbs_lon
                    # 未到 lbs_interval 时不请求，沿用当前 gps_data（可能为上次 LBS 或 None）
                if lat is not None and lon is not None and gps_data.get("_source") != "LBS":
                    gps_data["_source"] = "GNSS"
                # OLED 增量刷新（无定位也刷新；未接屏或异常时 oled_display 内部静默）
                if lat is not None and lon is not None:
                    lat_disp = "N%.5f" % lat if lat >= 0 else "S%.5f" % (-lat)
                    lon_disp = "E%.5f" % lon if lon >= 0 else "W%.5f" % (-lon)
                else:
                    lat_disp = "---"
                    lon_disp = "---"
                gnss_type = gps_data.get("_source") or "---"
                ts = last_report_ts if last_report_ts else now
                try:
                    loc = utime.localtime(ts)
                    update_time = "%02d:%02d" % (loc[3], loc[4])
                except Exception:
                    update_time = "--:--"
                time_dif = int(now - ts) if ts else 0
                speed_kmh = gps_data.get("speed") or 0
                bat_pct = None
                if battery:
                    try:
                        bat_pct, _ = battery.get_battery()
                    except Exception:
                        pass
                oled_display.update_position(
                    oled_i2c, lat_disp, lon_disp, gnss_type,
                    update_time, time_dif, speed_kmh, bat_pct
                )
                if lat is None or lon is None:
                    utime.sleep(1)
                    continue

                # APRS：有位置且间隔到时则入队（异步上报在 aprs_report.py）
                if aprs_report and aprs_cfg.get("aprs_callsign"):
                    aprs_interval = aprs_cfg.get("aprs_interval", 60)
                    if (now - last_aprs_ts) >= aprs_interval:
                        aprs_report.enqueue(gps_data)
                        last_aprs_ts = now

                # 2) 间隔：速度≤阈值按静止间隔，否则按运动间隔
                if now - last_report_ts < moving_interval:
                    utime.sleep(1)
                    continue
                last_report_ts = now
                speed_kmh = gps_data.get("speed") or 0
                if speed_kmh <= still_speed_threshold and (now - last_still_report_ts) < still_interval:
                    utime.sleep(1)
                    continue
                last_still_report_ts = now

                # 打点：构造 Traccar 载荷并入队（异步发送在 traccar_report.py）
                payload = build_traccar_payload(device_id, lat, lon, gps_data)
                #print(payload)
                if traccar_report:
                    traccar_report.enqueue(payload)

                utime.sleep(1)
            except Exception as loop_err:
                print("main_loop error:", loop_err)
                oled_status("err:" + str(loop_err)[:17])
                utime.sleep(2)
    finally:
        oled_status("exit.")
        oled_display.clear(oled_i2c)
        if wdt:
            try:
                wdt.stop()
            except Exception:
                pass
        try:
            quecgnss.gnssEnable(0)
        except Exception:
            pass
        print("GNSS_Reporter exit.")
        if _powerkey_exit_requested:
            Power.powerDown()


if __name__ == "__main__":
    main()
