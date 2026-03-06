# GNSS_Traccar.py - 移远 EC800M QuecPython 定位上报主程序
#
# 功能：从内置 GNSS 获取位置，按运动/静止策略上报到 Traccar；
#       配置从 config.cfg 读取，设备 ID 使用 IMEI；
#       弱网时使用持久化文件缓存；刷机控制引脚未悬空时退出。
#
# 参考：https://developer.quectel.com/doc/quecpython/API_reference/zh/

import utime
import uos
import ujson
import sys


# 将 /usr 加入 sys.path，以便 import battery（脚本与 battery.py 均放在 /usr 下）
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

try:
    import quecgnss
except Exception as e:
    print("quecgnss import failed:", e)
    raise SystemExit

import usocket as socket
import dataCall
import checkNet

try:
    import request as http_request
except Exception:
    http_request = None

try:
    import net
except Exception:
    net = None

try:
    from machine import Pin, WDT
except Exception as e:
    print("machine.Pin import failed:", e)
    raise SystemExit

try:
    import modem
except Exception as e:
    print("modem import failed:", e)
    raise SystemExit

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


# ------------------------- 默认配置 -------------------------
CONFIG_PATHS = ("config.cfg", "/usr/config.cfg")  # 依次尝试，找不到则用默认值
CID = 1
PROFILE = 0
RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)

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
    # 默认值
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
        "flash_gpio": int_(cfg.get("flash_gpio"), -1),  # -1 表示不检测刷机引脚
        "network_timeout": int_(cfg.get("network_timeout"), 60),
        "http_timeout": int_(cfg.get("http_timeout"), 10),
        "max_backoff": int_(cfg.get("max_backoff"), 60),
        "wdt_period": int_(cfg.get("wdt_period"), 60),  # 看门狗超时(秒)，0 表示不启用
    }

# ------------------------- 刷机引脚 -------------------------
def create_flash_pin(gpio_num):
    """创建刷机控制引脚：输入+上拉，悬空为高，接 GND 为低时表示刷机模式。gpio_num<0 表示禁用。返回 Pin 或 None。"""
    if gpio_num < 0:
        return None
    try:
        return Pin(gpio_num, Pin.IN, Pin.PULL_PU, 1)
    except Exception:
        return None

def is_flash_mode(pin):
    """引脚被拉低时返回 True，表示应退出进入刷机。"""
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

# ------------------------- 持久化缓存（按行 JSON）-------------------------
def _cache_exists(cache_path):
    try:
        uos.stat(cache_path)
        return True
    except Exception:
        return False

def ensure_cache_file(cache_path):
    """若缓存文件不存在则创建空文件，便于后续 append/read。"""
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
    """取队首条目的 next_ts，若队列空或文件不存在返回 0。"""
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

# 全局最新 GNSS 数据（与示例一致的字段名便于复用逻辑）
gps_data = {
    "lat": None,
    "lon": None,
    "speed": 0,       # km/h
    "track": None,
    "alt": None,
    "sats": 0,
    "hdop": None,
    "fix": "0",
}

def gnss_read_once():
    """读一次 quecgnss，解析并更新全局 gps_data。"""
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
                        gps_data["speed"] = float(spd_kn) * 1.852  # 节 -> km/h
                    except Exception:
                        gps_data["speed"] = 0
                    try:
                        gps_data["track"] = float(course) if course else None
                    except Exception:
                        gps_data["track"] = None

# ------------------------- 时间 -------------------------
def get_utc_timestamp():
    """QuecPython: localtime() 为 RTC 本地时间，mktime_ex() 转为 UTC 时间戳(秒)。"""
    try:
        ts = utime.mktime(utime.localtime())
        return ts
    except Exception:
        return 0

# ------------------------- Traccar 发送（request POST）-------------------------
def send_position(host, port, device_id, payload, timeout_s=10):
    """用 request.post 上报一条位置，data 为 JSON（支持嵌套如 network）。成功返回 True，可重试返回 'retry'，否则 False。"""
    if http_request is None:
        print("send_position: request module not available")
        return False
    url = "http://%s:%s/" % (host, port)
    payload["id"] = device_id
    try:
        resp = http_request.post(
            url,
            data=ujson.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
    except Exception as e:
        print("send_position error:", e)
        return "retry"
    try:
        code = getattr(resp, "status_code", None)
        if code is None:
            return False
        if code in (200, 204):
            return True
        if code in RETRYABLE_HTTP:
            return "retry"
        return False
    finally:
        try:
            resp.close()
        except Exception:
            pass

# ------------------------- 主流程 -------------------------
def main():
    print("GNSS_Traccar starting...")
    cfg = load_config()
    print("config:", cfg)

    # 刷机引脚：未悬空（如接 GND）则退出
    flash_pin = create_flash_pin(cfg["flash_gpio"])
    if is_flash_mode(flash_pin):
        print("Flash pin asserted, exit for flash mode.")
        raise SystemExit

    device_id = get_device_id()
    print("device_id:", device_id)

    # 网络
    print("wait network...")
    stagecode, subcode = checkNet.waitNetworkReady(cfg["network_timeout"])
    if stagecode != 3:
        print("network not ready, exit.")
        raise SystemExit
    print("network ready")

    # 确保数据通道已激活（避免首次 socket 报 ENOTCONN）
    try:
        dataCall.getInfo(CID, PROFILE)
        utime.sleep(1)
    except Exception as e:
        print("dataCall.getInfo:", e)

    # GNSS
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

    host = cfg["traccar_host"]
    port = cfg["traccar_port"]
    moving_interval = cfg["moving_interval"]
    still_interval = cfg["still_interval"]
    still_speed_threshold = cfg["still_speed_threshold"]
    cache_file = cfg["cache_file"]
    http_timeout = cfg["http_timeout"]
    max_backoff = cfg["max_backoff"]

    ensure_cache_file(cache_file)

    wdt = None
    if cfg["wdt_period"] > 0:
        try:
            wdt = WDT(cfg["wdt_period"])
            print("WDT started, period %d s" % cfg["wdt_period"])
        except Exception as e:
            print("WDT init failed:", e)

    last_report_ts = 0
    last_still_report_ts = 0
    tick = 0

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
                # 周期性检查刷机引脚
                if tick % 30 == 0 and is_flash_mode(flash_pin):
                    print("Flash pin asserted, exit.")
                    break

                # 读 GNSS
                gnss_read_once()

                # 先消费缓存（弱网时积压的）
                next_ts = cache_peek_next_ts(cache_file)
                if next_ts <= now:
                    item = cache_pop(cache_file)
                    if item:
                        payload = item.get("payload", {})
                        attempts = item.get("attempts", 0)
                        r = send_position(host, port, device_id, payload, http_timeout)
                        if r is True:
                            print("Traccar Sent Cache Success: %s %s" % ("%.6f" % float(payload.get("lat", 0)), "%.6f" % float(payload.get("lon", 0))))
                        elif r == "retry":
                            attempts += 1
                            backoff = min(max_backoff, attempts * 5)
                            item["attempts"] = attempts
                            item["next_ts"] = now + backoff
                            cache_push(cache_file, item)
                            print("Traccar retry later, backoff", backoff)

                # 按间隔决定是否产生新点
                if now - last_report_ts < moving_interval:
                    utime.sleep(1)
                    continue
                last_report_ts = now

                lat = gps_data.get("lat")
                lon = gps_data.get("lon")
                if lat is None or lon is None or gps_data.get("fix") == "0":
                    utime.sleep(1)
                    continue

                # 静止检测
                speed_kmh = gps_data.get("speed") or 0
                if speed_kmh <= still_speed_threshold and (now - last_still_report_ts) < still_interval:
                    utime.sleep(1)
                    continue
                last_still_report_ts = now

                # 构造 Traccar 载荷
                payload = {
                    "id": device_id,
                    "lat": "%.7f" % lat,
                    "lon": "%.7f" % lon,
                    "timestamp": get_utc_timestamp(),
                }
                if gps_data.get("speed") is not None:
                    payload["speed"] = "%.2f" % (float(gps_data["speed"]) / 1.852)
                if gps_data.get("track") is not None:
                    payload["bearing"] = "%.1f" % float(gps_data["track"])
                if gps_data.get("alt") is not None:
                    payload["altitude"] = "%.1f" % float(gps_data["alt"])
                if gps_data.get("sats") is not None:
                    payload["sat"] = gps_data["sats"]
                if battery:
                    level, voltage = battery.get_battery()
                    if level is not None:
                        payload["batteryLevel"] = "%.1f" % level
                    if voltage is not None:
                        payload["batteryVoltage"] = voltage
                if net:
                    try:
                        payload["rssi"] = net.csqQueryPoll()
                        payload["network"] = cell_info.get_cell_info_json()
                    except Exception:
                        pass

                # 先尝试直接发送，失败则写入持久化缓存
                r = send_position(host, port, device_id, payload, http_timeout)
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
        if wdt:
            try:
                wdt.stop()
            except Exception:
                pass
        try:
            quecgnss.gnssEnable(0)
        except Exception:
            pass
        print("GNSS_Traccar exit.")


if __name__ == "__main__":
    main()
