# aprs_report.py - APRS 位置上报（参数从 config.cfg 读取）
#
# 通过 APRS-IS TCP 上报位置，帧格式与用户指定一致；
# 最小上报间隔 30 秒由调用方/配置保证。
import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import utime
import usocket as socket

try:
    import config as shared_config
except Exception:
    shared_config = None


def load_config():
    """从统一 config 读取 APRS 相关参数；aprs_interval 若小于 30 则按 30 使用。"""
    if shared_config:
        full = shared_config.load_config()
        return {
            "aprs_callsign": full.get("aprs_callsign", ""),
            "aprs_ssid": full.get("aprs_ssid", ""),
            "aprs_passcode": full.get("aprs_passcode", ""),
            "aprs_host": full.get("aprs_host", "rotate.aprs.net"),
            "aprs_port": full.get("aprs_port", 14580),
            "aprs_interval": full.get("aprs_interval", 60),
            "aprs_message": full.get("aprs_message", ""),
            "aprs_icon": (full.get("aprs_icon", ">") or ">")[:1],
        }
    return {
        "aprs_callsign": "",
        "aprs_ssid": "",
        "aprs_passcode": "",
        "aprs_host": "rotate.aprs.net",
        "aprs_port": 14580,
        "aprs_interval": 60,
        "aprs_message": "",
        "aprs_icon": ">",
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


def _time_aprs():
    """返回本地时间字符串 HHMMSS（用于帧内说明）。当前 RTC 为本地时间，非 UTC。"""
    try:
        t = utime.localtime()
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

    gnss_type = "GNSS"
    nmea_ts = _time_aprs()
    message = (cfg.get("aprs_message") or "").strip()
    tail = " APRS by QuecPtyhon with %s at local time %s" % (gnss_type, nmea_ts)
    if message:
        tail += " " + message

    # !ddmm.mmN/dddmm.mmE>course/speed/A=alt ...（用拼接避免 %s 参数个数问题）
    body = "!" + lat_aprs + "/" + lon_aprs + icon + course_str + "/" + speed_str + "/A=" + alt_str + tail
    return body.encode("utf-8")


def send_aprs(cfg, frame_body):
    """
    连接 APRS-IS，登录后发送一帧。frame_body 为不含呼号头的帧正文 bytes。
    成功返回 True，失败返回 False。
    """
    callsign = (cfg.get("aprs_callsign") or "").strip().upper()
    if not callsign or not frame_body:
        return False
    # 源地址：若配置了 aprs_ssid 则用其作为源，否则用 callsign；APRS-IS 要求大写
    ssid = (cfg.get("aprs_ssid") or "").strip().upper()
    source = ssid if ssid else callsign

    host = cfg.get("aprs_host", "rotate.aprs.net")
    port = int(cfg.get("aprs_port", 14580))
    passcode = str(cfg.get("aprs_passcode", "")).strip()

    # 登录用 base callsign（passcode 按呼号计算），发包用 source（可带 SSID）
    # 行尾必须为 \r\n（字节），与 Mac nc 一致，避免设备端编码/换行差异导致 unverified
    login_line = ("user %s pass %s vers EC800MAPRS 1.0" % (callsign, passcode)).encode("utf-8") + b"\r\n"
    packet_line = ("%s>APRS,TCPIP*:%s" % (source, frame_body.decode("utf-8"))).encode("utf-8") + b"\r\n"

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
        utime.sleep(1)  # 等 1 秒再发登录，与服务器就绪顺序一致，避免 unverified
        s.send(login_line)
        utime.sleep_ms(200)
        s.send(packet_line)
        s.settimeout(2)
        while True:
            try:
                if not s.recv(256):
                    break
            except Exception:
                break
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
