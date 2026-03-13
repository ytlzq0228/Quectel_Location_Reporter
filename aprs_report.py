# aprs_report.py - APRS 位置上报（生产-消费异步 + 持久化）
#
# 主程序只调用 enqueue(gps_data) 打点；本模块内维护系统 Queue，生产者只写队列，消费者只读队列（RETRY 时改 next_ts 后写回队列）。
# 备份线程定期将队列全量同步到持久化文件（全 pop → 写文件 → 全 put 回）。重启时从文件加载到队列。
# 以上逻辑与 traccar_report 一致，对主进程调用无影响。

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import utime
import ujson
import usocket as socket
import _thread
import log

_log = log.getLogger("APRS")

try:
    import config as shared_config
except Exception:
    shared_config = None

try:
    from queue import Queue
except Exception:
    Queue = None

APRS_RETRY_BACKOFF_BASE_SEC = 5
APRS_MAX_BACKOFF = 60

# 持久化文件路径写死，不从配置读取
APRS_CACHE_FILE = "/usr/aprs_cache.txt"
APRS_BACKUP_INTERVAL_SEC = 30

# 模块内状态：由 start_consumer 初始化
_aprs_queue = None
_aprs_cfg = None


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
        _log.error("APRS getaddrinfo error: %s" % e)
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
        _log.error("APRS send error: %s" % e)
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


# ------------------------- 备份线程：定期全量同步队列到文件 -------------------------
def _cache_exists(path):
    try:
        import uos
        uos.stat(path)
        return True
    except Exception:
        return False


def _backup_loop():
    """备份者线程：定期将队列全量 pop → 写文件 → 全 put 回。"""
    global _aprs_queue
    while True:
        utime.sleep(APRS_BACKUP_INTERVAL_SEC)
        if _aprs_queue is None:
            continue
        L = []
        while not _aprs_queue.empty():
            try:
                L.append(_aprs_queue.get())
            except Exception:
                break
        if not L:
            try:
                with open(APRS_CACHE_FILE, "w") as f:
                    pass
            except Exception:
                pass
            continue
        try:
            with open(APRS_CACHE_FILE, "w") as f:
                for item in L:
                    f.write(ujson.dumps(item) + "\n")
        except Exception as e:
            _log.error("aprs backup write error: %s" % e)
        for item in L:
            try:
                _aprs_queue.put(item)
            except Exception as e:
                _log.error("aprs backup put back error: %s" % e)


# ------------------------- 消费者线程：只读队列，RETRY 时改 next_ts 后写回队列 -------------------------
def _consumer_loop():
    """消费者：只从队列取；发送成功则结束；失败则改 next_ts 后 put 回队列。不操作持久化文件。"""
    global _aprs_queue, _aprs_cfg
    if _aprs_queue is None or _aprs_cfg is None:
        return
    now = utime.time
    while True:
        item = None
        try:
            if not _aprs_queue.empty():
                item = _aprs_queue.get()
        except Exception:
            pass
        if item is None:
            utime.sleep(1)
            continue

        next_ts = float(item.get("next_ts", 0))
        if next_ts > 0 and now() < next_ts:
            try:
                _aprs_queue.put(item)
            except Exception:
                pass
            utime.sleep(1)
            continue

        gps_data = item.get("gps_data") or {}
        if gps_data.get("lat") is None or gps_data.get("lon") is None:
            utime.sleep(0)
            continue
        frame_body = build_aprs_frame(gps_data, _aprs_cfg)
        if not frame_body:
            utime.sleep(0)
            continue
        ok = send_aprs(_aprs_cfg, frame_body)
        if ok:
            lat, lon = gps_data.get("lat"), gps_data.get("lon")
            _log.info("APRS Sent: %.6f %.6f" % (float(lat), float(lon)))
        else:
            attempts = item.get("attempts", 0) + 1
            backoff = min(APRS_MAX_BACKOFF, attempts * APRS_RETRY_BACKOFF_BASE_SEC)
            item["attempts"] = attempts
            item["next_ts"] = now() + backoff
            try:
                _aprs_queue.put(item)
            except Exception as e:
                _log.error("aprs retry put back error: %s" % e)
            _log.warning("APRS retry later, backoff %s" % backoff)
        utime.sleep(0)


def start_consumer(cfg):
    """
    启动 APRS 消费者线程与备份线程。传入 load_config() 的返回值；主程序之后只调用 enqueue(gps_data)。
    启动时从持久化文件加载到队列（若存在），再启动消费者与备份者。
    """
    global _aprs_queue, _aprs_cfg
    if Queue is None:
        _log.warning("aprs_report: Queue not available, enqueue will no-op")
        return
    if not (cfg.get("aprs_callsign") or "").strip():
        return
    _aprs_cfg = cfg
    _aprs_queue = Queue(100)

    # 启动时从持久化文件加载到队列
    if _cache_exists(APRS_CACHE_FILE):
        try:
            with open(APRS_CACHE_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = ujson.loads(line)
                        _aprs_queue.put(item)
                    except Exception as e:
                        _log.warning("aprs load cache line error: %s" % e)
        except Exception as e:
            _log.warning("aprs load cache error: %s" % e)

    try:
        _thread.start_new_thread(_consumer_loop, ())
        _log.info("APRS consumer thread started")
    except Exception as e:
        _log.error("APRS start_consumer error: %s" % e)

    try:
        _thread.start_new_thread(_backup_loop, ())
        _log.info("APRS backup thread started")
    except Exception as e:
        _log.error("APRS start_backup error: %s" % e)


def enqueue(gps_data):
    """
    生产：仅将一条位置入队。主程序只负责按间隔调用此接口打点；不写持久化文件。
    """
    global _aprs_queue, _aprs_cfg
    if _aprs_queue is None or _aprs_cfg is None:
        return
    if gps_data.get("lat") is None or gps_data.get("lon") is None:
        return
    item = {"gps_data": gps_data, "attempts": 0, "next_ts": 0}
    try:
        _aprs_queue.put(item)
    except Exception as e:
        _log.error("aprs enqueue put error: %s" % e)
    _log.info("APRS Cached: %.6f %.6f" % (float(gps_data.get("lat")), float(gps_data.get("lon"))))
