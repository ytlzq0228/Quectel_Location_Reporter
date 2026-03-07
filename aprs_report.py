# aprs_report.py - APRS 位置上报（参数从 config.cfg 读取）
#
# 通过 APRS-IS TCP 上报位置，帧格式与用户指定一致；
# 最小上报间隔 30 秒由调用方/配置保证。

import utime
import usocket as socket

# 配置路径与 GNSS_Traccar 一致
CONFIG_PATHS = ("config.cfg", "/usr/config.cfg")

APRS_MIN_INTERVAL = 30


def load_config():
    """从 cfg 读取 APRS 相关参数；aprs_interval 若小于 30 则按 30 使用。"""
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

    raw_interval = int_(cfg.get("aprs_interval"), 60)
    aprs_interval = max(APRS_MIN_INTERVAL, raw_interval)

    return {
        "aprs_callsign": cfg.get("aprs_callsign", "").strip(),
        "aprs_passcode": cfg.get("aprs_passcode", ""),
        "aprs_host": cfg.get("aprs_host", "rotate.aprs.net"),
        "aprs_port": int_(cfg.get("aprs_port"), 14580),
        "aprs_interval": aprs_interval,
        "aprs_message": cfg.get("aprs_message", "").strip(),
        "aprs_icon": (cfg.get("aprs_icon", ">") or ">")[:1],
    }


def _deg_to_aprs_lat(deg):
    """纬度十进制度 -> APRS 格式 ddmm.mmN/S"""
    if deg is None:
        return "0000.00N"
    d = int(abs(deg))
    m = (abs(deg) - d) * 60.0
    s = "%02d%05.2f" % (d, m)
    return s + ("N" if deg >= 0 else "S")


def _deg_to_aprs_lon(deg):
    """经度十进制度 -> APRS 格式 dddmm.mmE/W"""
    if deg is None:
        return "00000.00E"
    d = int(abs(deg))
    m = (abs(deg) - d) * 60.0
    s = "%03d%05.2f" % (d, m)
    return s + ("E" if deg >= 0 else "W")


def _utc_time_aprs():
    """返回 UTC 时间字符串 HHMMSS（用于帧内说明）。"""
    try:
        t = utime.localtime()
        # 若 RTC 为本地时间，此处简化为直接格式；若有 UTC 接口可改为 UTC
        return "%02d%02d%02d" % (t[3], t[4], t[5])
    except Exception:
        return "000000"


def build_aprs_frame(gps_data, cfg):
    """
    根据 gps_data 和 cfg 构造 APRS 位置帧正文（不含呼号头）。
    格式: !{lat}{lat_dir}/{lon}{lon_dir}{icon}{course}/{speed}/A={altitude} ...
    返回 bytes，无有效位置时返回 None。
    """
    lat = gps_data.get("lat")
    lon = gps_data.get("lon")
    if lat is None or lon is None:
        return None

    lat_aprs = _deg_to_aprs_lat(lat)
    lon_aprs = _deg_to_aprs_lon(lon)
    icon = cfg.get("aprs_icon", ">")

    try:
        course = int(round(float(gps_data.get("track") or 0))) % 360
    except Exception:
        course = 0
    course_str = "%03d" % course

    # 速度：km/h -> 节（knots），APRS 常用 3 位
    try:
        speed_kmh = float(gps_data.get("speed") or 0)
        speed_kn = speed_kmh / 1.852
        speed_str = "%03d" % min(999, int(round(speed_kn)))
    except Exception:
        speed_str = "000"

    # 海拔 m -> feet
    try:
        alt_m = float(gps_data.get("alt") or 0)
        alt_ft = int(round(alt_m * 3.28084))
        alt_str = str(max(0, min(999999, alt_ft)))
    except Exception:
        alt_str = "0"

    gnss_type = "GPS"
    nmea_ts = _utc_time_aprs()
    message = (cfg.get("aprs_message") or "").strip()
    tail = " APRS by RPI with GNSS %s at UTC %s" % (gnss_type, nmea_ts)
    if message:
        tail += " " + message

    # !ddmm.mmN/dddmm.mmE>course/speed/A=alt ...
    body = "!%s/%s%s%s/%s/%s/A=%s" % (
        lat_aprs,
        lon_aprs,
        icon,
        course_str,
        speed_str,
        alt_str,
    ) + tail
    return body.encode("utf-8")


def send_aprs(cfg, frame_body):
    """
    连接 APRS-IS，登录后发送一帧。frame_body 为不含呼号头的帧正文 bytes。
    成功返回 True，失败返回 False。
    """
    callsign = (cfg.get("aprs_callsign") or "").strip()
    if not callsign or not frame_body:
        return False

    host = cfg.get("aprs_host", "rotate.aprs.net")
    port = int(cfg.get("aprs_port", 14580))
    passcode = str(cfg.get("aprs_passcode", ""))

    # 登录行: user CALLSIGN pass PASSCODE vers Software Version
    login = "user %s pass %s vers EC800M-APRS 1.0\r\n" % (callsign, passcode)
    # 发送行: CALLSIGN>APRS,TCPIP*:frame_body
    packet = "%s>APRS,TCPIP*:%s\r\n" % (callsign, frame_body.decode("utf-8"))

    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
    except Exception as e:
        print("APRS getaddrinfo error:", e)
        return False

    s = None
    try:
        s = socket.socket()
        s.settimeout(15)
        s.connect(addr)
        s.send(login.encode("utf-8"))
        utime.sleep_ms(200)
        s.send(packet.encode("utf-8"))
        utime.sleep_ms(100)
        return True
    except Exception as e:
        print("APRS send error:", e)
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass
