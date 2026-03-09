# GNSS_Reporter.py - 移远 EC800M QuecPython 定位上报主循环与逻辑
#
# 功能：优先 GNSS 定位；无 GNSS 时用 LBS 基站定位；按运动/静止策略上报 Traccar，按间隔上报 APRS；
#       配置从 config.cfg 读取，设备 ID 使用 IMEI；弱网时持久化缓存；刷机引脚未悬空时退出。
#       LBS 定位时按静止间隔上报。APRS 在 aprs_report.py，Traccar 在 traccar_report.py。

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

import utime
import uos
import net
import ujson
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

import traccar_report

try:
    import oled_display
except Exception as e:
    print("oled_display import failed:", e)
    oled_display = None

# ------------------------- 默认配置 -------------------------
CONFIG_PATHS = ("config.cfg", "/usr/config.cfg")
CID = 1
PROFILE = 0


# ------------------------- 配置读取 -------------------------
def load_config():
    cfg = {}
    for path in CONFIG_PATHS:
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip()
            break
        except Exception:
            pass

    def int_(v, d):
        try:
            return int(v)
        except Exception:
            return d

    return {
        "traccar_host": cfg.get("traccar_host", "traccar.example.com"),
        "traccar_port": int_(cfg.get("traccar_port"), 5055),
        "moving_interval": int_(cfg.get("moving_interval"), 10),
        "still_interval": int_(cfg.get("still_interval"), 300),
        "still_speed_threshold": int_(cfg.get("still_speed_threshold"), 5),
        "cache_file": cfg.get("cache_file", "/usr/traccar_cache.txt"),
        "flash_gpio": int_(cfg.get("flash_gpio"), -1),
        "network_timeout": int_(cfg.get("network_timeout"), 60),
        "http_timeout": int_(cfg.get("http_timeout"), 10),
        "max_backoff": int_(cfg.get("max_backoff"), 60),
        "wdt_period": int_(cfg.get("wdt_period"), 60),
        "lbs_server": cfg.get("lbs_server", "").strip(),
        "lbs_port": int_(cfg.get("lbs_port"), 80),
        "lbs_token": cfg.get("lbs_token", "").strip(),
        "lbs_timeout": max(1, min(300, int_(cfg.get("lbs_timeout"), 30))),
        "lbs_profile_idx": max(1, min(3, int_(cfg.get("lbs_profile_idx"), 1))),
        "lbs_interval": max(10, int_(cfg.get("lbs_interval"), 60)),  # 两次 LBS 请求最小间隔（秒），按次计费时宜设大
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


# ------------------------- 持久化缓存 -------------------------
def _cache_exists(cache_path):
    try:
        uos.stat(cache_path)
        return True
    except Exception:
        return False


def ensure_cache_file(cache_path):
    if _cache_exists(cache_path):
        return
    try:
        with open(cache_path, "w") as f:
            pass
    except Exception as e:
        print("ensure_cache_file error:", e)


def cache_push(cache_path, item):
    try:
        with open(cache_path, "a") as f:
            f.write(ujson.dumps(item) + "\n")
    except Exception as e:
        print("cache_push error:", e)


def cache_pop(cache_path):
    if not _cache_exists(cache_path):
        return None
    try:
        with open(cache_path, "r") as f:
            lines = f.readlines()
        if not lines:
            return None
        first = lines[0].strip()
        rest = lines[1:]
        with open(cache_path, "w") as f:
            f.write("".join(rest))
        return ujson.loads(first)
    except Exception as e:
        print("cache_pop error:", e)
    return None


def cache_peek_next_ts(cache_path):
    if not _cache_exists(cache_path):
        return 0
    try:
        with open(cache_path, "r") as f:
            line = f.readline()
        if not line:
            return 0
        item = ujson.loads(line.strip())
        return float(item.get("next_ts", 0))
    except Exception:
        return 0


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
    cfg = load_config()
    print("config:", cfg)

    flash_pin = create_flash_pin(cfg["flash_gpio"])
    if is_flash_mode(flash_pin):
        print("Flash pin asserted, exit for flash mode.")
        raise SystemExit

    device_id = get_device_id()
    print("device_id:", device_id)

    print("wait network...")
    stagecode, subcode = checkNet.waitNetworkReady(cfg["network_timeout"])
    if stagecode != 3:
        print("network not ready, exit.")
        raise SystemExit
    print("network ready")

    try:
        dataCall.getInfo(CID, PROFILE)
        utime.sleep(1)
    except Exception as e:
        print("dataCall.getInfo:", e)

    try:
        quecgnss.configSet(0, 1)
        quecgnss.configSet(2, 1)
        quecgnss.configSet(4, 1)
    except Exception as e:
        print("quecgnss configSet:", e)
    ret = quecgnss.init()
    if ret != 0:
        print("GNSS init failed, ret:", ret)
        raise SystemExit
    print("GNSS init ok")

    oled_i2c = None
    if oled_display:
        oled_i2c = oled_display.init_oled()
        if oled_i2c:
            print("OLED init ok")

    host = cfg["traccar_host"]
    port = cfg["traccar_port"]
    moving_interval = cfg["moving_interval"]
    still_interval = cfg["still_interval"]
    still_speed_threshold = cfg["still_speed_threshold"]
    cache_file = cfg["cache_file"]
    http_timeout = cfg["http_timeout"]
    max_backoff = cfg["max_backoff"]

    ensure_cache_file(cache_file)

    aprs_cfg = aprs_report.load_config() if aprs_report else {}
    last_aprs_ts = 0

    wdt = None
    if cfg["wdt_period"] > 0:
        try:
            wdt = WDT(cfg["wdt_period"])
            print("WDT started, period %d s" % cfg["wdt_period"])
        except Exception as e:
            print("WDT init failed:", e)

    last_report_ts = 0
    last_still_report_ts = 0
    last_lbs_ts = 0
    tick = 0

    try:
        pk = PowerKey()
        if pk.powerKeyEventRegister(_powerkey_callback) == 0:
            print("PowerKey registered: long press >= 1s to exit.")
        else:
            print("PowerKey register failed.")
    except Exception as e:
        print("PowerKey init error:", e)

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
                    break
                if tick % 30 == 0 and is_flash_mode(flash_pin):
                    print("Flash pin asserted, exit.")
                    break

                gnss_read_once()
                #print(gps_data)

                # APRS：有位置且间隔到时则上报（能力在 aprs_report.py）
                if aprs_report and aprs_cfg.get("aprs_callsign"):
                    lat = gps_data.get("lat")
                    lon = gps_data.get("lon")
                    aprs_interval = aprs_cfg.get("aprs_interval", 60)
                    if lat is not None and lon is not None and (now - last_aprs_ts) >= aprs_interval:
                        frame_body = aprs_report.build_aprs_frame(gps_data, aprs_cfg)
                        if frame_body and aprs_report.send_aprs(aprs_cfg, frame_body):
                            last_aprs_ts = now
                            print("APRS Sent: %.6f %.6f" % (lat, lon))

                # 消费 Traccar 缓存（能力在 traccar_report.py）
                next_ts = cache_peek_next_ts(cache_file)
                if next_ts <= now:
                    item = cache_pop(cache_file)
                    if item:
                        payload = item.get("payload", {})
                        attempts = item.get("attempts", 0)
                        r = traccar_report.send_position(host, port, device_id, payload, http_timeout)
                        if r is True:
                            print("Traccar Sent Cache Success: %s %s" % (
                                "%.6f" % float(payload.get("lat", 0)),
                                "%.6f" % float(payload.get("lon", 0)),
                            ))
                        elif r == "retry":
                            attempts += 1
                            backoff = min(max_backoff, attempts * 5)
                            item["attempts"] = attempts
                            item["next_ts"] = now + backoff
                            cache_push(cache_file, item)
                            print("Traccar retry later, backoff", backoff)

                # 1) 优先 GNSS；无有效 lat/lon 时按间隔尝试 LBS（LBS 按次计费，用 lbs_interval 限频）
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
                # OLED 增量刷新（有则更新，无定位也刷新显示）
                if oled_display and oled_i2c:
                    try:
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
                    except Exception as oled_err:
                        print("oled update error:", oled_err)
                if lat is None or lon is None:
                    utime.sleep(1)
                    continue

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

                # 构造 Traccar 载荷并上报（发送能力在 traccar_report.py）
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
                    level, voltage = battery.get_battery()
                    if level is not None:
                        payload["batteryLevel"] = "%.1f" % level
                    if voltage is not None:
                        payload["batteryVoltage"] = voltage
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
                print(payload)
                r = traccar_report.send_position(host, port, device_id, payload, http_timeout)
                if r is True:
                    print("Traccar Sent Success: %.6f %.6f" % (lat, lon))
                else:
                    item = {"payload": payload, "attempts": 0, "next_ts": 0}
                    if r == "retry":
                        item["attempts"] = 1
                        item["next_ts"] = now + 5
                    cache_push(cache_file, item)
                    print("Traccar Cached: %.6f %.6f" % (lat, lon))

                utime.sleep(1)
            except Exception as loop_err:
                print("main_loop error:", loop_err)
                utime.sleep(2)
    finally:
        if oled_display and oled_i2c:
            try:
                oled_display.clear(oled_i2c)
            except Exception as e:
                print("oled clear on exit:", e)
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
        Power.powerDown()


if __name__ == "__main__":
    main()
