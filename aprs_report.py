# aprs_report.py - APRS 位置上报（生产-消费异步 + 持久化）
#
# 主程序只调用 enqueue(gps_data) 打点；本模块内维护 Queue + 文件缓存，消费者线程异步上报。
import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
import utime
import ujson
import usocket as socket
import _thread

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

# 模块内状态：由 start_consumer 初始化
_aprs_aprs_queue = None
_aprs_aprs_file_lock = None
_aprs_cfg = None
_aprs_cache_file = None


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


# ------------------------- 持久化缓存（内部，需持锁调用）-------------------------
def _cache_exists(path):
    try:
        import uos
        uos.stat(path)
        return True
    except Exception:
        return False


def _cache_ensure(path):
    if _cache_exists(path):
        return
    try:
        with open(path, "w") as f:
            pass
    except Exception as e:
        print("aprs_cache ensure error:", e)


def _cache_push_locked(path, item):
    try:
        with open(path, "a") as f:
            f.write(ujson.dumps(item) + "\n")
    except Exception as e:
        print("aprs_cache push error:", e)


def _cache_pop_locked(path):
    if not _cache_exists(path):
        return None
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        if not lines:
            return None
        first = lines[0].strip()
        rest = lines[1:]
        with open(path, "w") as f:
            f.write("".join(rest))
        return ujson.loads(first)
    except Exception as e:
        print("aprs_cache pop error:", e)
    return None


def _cache_peek_next_ts_locked(path):
    if not _cache_exists(path):
        return 0
    try:
        with open(path, "r") as f:
            line = f.readline()
        if not line:
            return 0
        item = ujson.loads(line.strip())
        return float(item.get("next_ts", 0))
    except Exception:
        return 0


def _consumer_loop():
    """消费者线程：从队列或文件取项，构造帧并发送；失败则写回文件带退避。"""
    global _aprs_queue, _aprs_file_lock, _aprs_cfg, _aprs_cache_file
    if _aprs_queue is None or _aprs_file_lock is None or _aprs_cfg is None or _aprs_cache_file is None:
        return
    now = utime.time
    while True:
        item = None
        from_aprs_queue = False
        if not _aprs_queue.empty():
            try:
                item = _aprs_queue.get()
                from_aprs_queue = True
            except Exception:
                pass
        if item is None:
            _aprs_file_lock.acquire()
            try:
                next_ts = _cache_peek_next_ts_locked(_aprs_cache_file)
                if next_ts <= now():
                    item = _cache_pop_locked(_aprs_cache_file)
            except Exception:
                pass
            try:
                _aprs_file_lock.release()
            except Exception:
                pass
        if item is None:
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
            if from_aprs_queue:
                _aprs_file_lock.acquire()
                try:
                    _cache_pop_locked(_aprs_cache_file)
                except Exception:
                    pass
                try:
                    _aprs_file_lock.release()
                except Exception:
                    pass
            lat, lon = gps_data.get("lat"), gps_data.get("lon")
            print("APRS Sent: %.6f %.6f" % (float(lat), float(lon)))
        else:
            attempts = item.get("attempts", 0) + 1
            backoff = min(APRS_MAX_BACKOFF, attempts * APRS_RETRY_BACKOFF_BASE_SEC)
            item["attempts"] = attempts
            item["next_ts"] = now() + backoff
            _aprs_file_lock.acquire()
            try:
                _cache_push_locked(_aprs_cache_file, item)
            finally:
                try:
                    _aprs_file_lock.release()
                except Exception:
                    pass
            print("APRS retry later, backoff", backoff)
        utime.sleep(0)


def start_consumer(cfg):
    """
    启动 APRS 消费者线程。传入 load_config() 的返回值；之后主程序只调用 enqueue(gps_data)。
    """
    global _aprs_queue, _aprs_file_lock, _aprs_cfg, _aprs_cache_file
    if Queue is None:
        print("aprs_report: Queue not available, enqueue will no-op")
        return
    if not (cfg.get("aprs_callsign") or "").strip():
        return
    _aprs_cfg = cfg
    _aprs_cache_file = "/usr/aprs_cache.txt"
    _aprs_file_lock = _thread.allocate_lock()
    _aprs_queue = Queue(100)
    _cache_ensure(_aprs_cache_file)

    _aprs_file_lock.acquire()
    try:
        while _cache_exists(_aprs_cache_file):
            next_ts = _cache_peek_next_ts_locked(_aprs_cache_file)
            if next_ts > utime.time():
                break
            item = _cache_pop_locked(_aprs_cache_file)
            if item is None:
                break
            try:
                if not _aprs_queue.put(item):
                    _cache_push_locked(_aprs_cache_file, item)
                    break
            except Exception:
                _cache_push_locked(_aprs_cache_file, item)
                break
    except Exception as e:
        print("aprs load cache error:", e)
    finally:
        try:
            _aprs_file_lock.release()
        except Exception:
            pass

    try:
        _thread.start_new_thread(_consumer_loop, ())
        print("APRS consumer thread started")
    except Exception as e:
        print("APRS start_consumer error:", e)


def enqueue(gps_data):
    """
    生产：将一条位置入队并追加到持久化文件。主程序只负责按间隔调用此接口打点。
    """
    global _aprs_queue, _aprs_file_lock, _aprs_cfg, _aprs_cache_file
    if _aprs_queue is None or _aprs_cfg is None or _aprs_cache_file is None:
        return
    if gps_data.get("lat") is None or gps_data.get("lon") is None:
        return
    item = {"gps_data": gps_data, "attempts": 0, "next_ts": 0}
    _aprs_file_lock.acquire()
    try:
        _cache_push_locked(_aprs_cache_file, item)
    finally:
        try:
            _aprs_file_lock.release()
        except Exception:
            pass
    try:
        _aprs_queue.put(item)
    except Exception as e:
        print("aprs enqueue put error:", e)
    print("APRS Cached: %.6f %.6f" % (float(gps_data.get("lat")), float(gps_data.get("lon"))))
