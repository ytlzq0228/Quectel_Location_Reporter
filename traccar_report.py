# traccar_report.py - Traccar 位置上报（生产-消费异步 + 持久化）
#
# 主程序只调用 enqueue(payload) 打点；本模块内维护系统 Queue，生产者只写队列，消费者只读队列（RETRY 时改 next_ts 后写回队列）。
# 备份线程定期将队列全量同步到持久化文件（全 pop → 写文件 → 全 put 回）。重启时从文件加载到队列。
# 以上逻辑在模块内闭环，对主进程调用无影响。

import utime
import ujson
import usocket as socket
import _thread
import log

_log = log.getLogger("Traccar")

try:
    import config as shared_config
except Exception:
    shared_config = None

try:
    from queue import Queue
except Exception:
    Queue = None

RETRYABLE_HTTP = (400, 408, 429, 500, 502, 503, 504)
SEND_OK = True
SEND_RETRY = "retry"
RETRY_BACKOFF_BASE_SEC = 5

# 持久化文件路径写死，不从配置读取
TRACCAR_CACHE_FILE = "/usr/traccar_cache.txt"
BACKUP_INTERVAL_SEC = 30

# 模块内状态：由 start_consumer 初始化
_traccar_queue = None
_traccar_consumer_params = None  # (traccar_host, traccar_port, device_id, traccar_http_timeout, traccar_max_backoff)


def load_config():
    """从统一 config 读取 Traccar 相关参数，返回带 traccar_ 前缀的配置子集。"""
    if shared_config:
        full = shared_config.load_config()
        return {
            "traccar_host": full.get("traccar_host", ""),
            "traccar_port": full.get("traccar_port", 5055),
            "traccar_max_backoff": full.get("traccar_max_backoff", 60),
        }
    return {
        "traccar_host": "",
        "traccar_port": 5055,
        "traccar_max_backoff": 60,
    }


def send_position(host, port, device_id, payload, timeout_s=10):
    """用 GET 请求上报一条位置。成功返回 SEND_OK，可重试错误返回 SEND_RETRY，其它失败返回 False。"""
    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
    except Exception as e:
        _log.error("getaddrinfo error: %s" % e)
        return False

    parts = ["id=" + str(device_id)]
    for k, v in payload.items():
        if k == "id":
            continue
        if v is not None and v != "":
            parts.append(k + "=" + str(v))
    qs = "&".join(parts)
    path = "/?" + qs
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%s\r\n"
        "User-Agent: EC800M-GNSS-Traccar\r\n"
        "Connection: close\r\n\r\n"
    ) % (path, host, port)

    def _do_send():
        s = socket.socket()
        try:
            s.settimeout(timeout_s)
            s.connect(addr)
            s.send(req.encode())
            resp = b""
            while True:
                try:
                    buf = s.recv(256)
                except OSError as e:
                    err = e.args[0] if e.args else 0
                    if err == 107 and len(resp) > 0:
                        break
                    raise
                if not buf:
                    break
                resp += buf
            return resp
        finally:
            try:
                s.close()
            except Exception:
                pass

    resp = None
    try:
        resp = _do_send()
    except OSError as e:
        err = e.args[0] if e.args else 0
        if err == 107:
            utime.sleep(2)
            try:
                resp = _do_send()
            except Exception as e2:
                _log.warning("send_position error (retry): %s" % e2)
                return SEND_RETRY
        else:
            _log.error("send_position error: %s" % e)
            return SEND_RETRY
    except Exception as e:
        _log.error("send_position error: %s" % e)
        return SEND_RETRY
    if resp is not None:
        head = resp[:12].decode("utf-8", "ignore")
        if "200" in head or "204" in head:
            return SEND_OK
        if any(str(c) in head for c in RETRYABLE_HTTP):
            return SEND_RETRY
    _log.error("send_position error: not retryable %s" % resp.decode("utf-8", "ignore"))
    return False


# ------------------------- 备份线程：定期全量同步队列到文件 -------------------------
def _cache_exists(cache_path):
    try:
        import uos
        uos.stat(cache_path)
        return True
    except Exception:
        return False


def _backup_loop():
    """备份者线程：定期将队列全量 pop → 写文件 → 全 put 回。"""
    global _traccar_queue
    while True:
        utime.sleep(BACKUP_INTERVAL_SEC)
        if _traccar_queue is None:
            continue
        L = []
        while not _traccar_queue.empty():
            try:
                L.append(_traccar_queue.get())
            except Exception:
                break
        if not L:
            try:
                with open(TRACCAR_CACHE_FILE, "w") as f:
                    pass
            except Exception:
                pass
            continue
        try:
            with open(TRACCAR_CACHE_FILE, "w") as f:
                for item in L:
                    f.write(ujson.dumps(item) + "\n")
        except Exception as e:
            _log.error("traccar backup write error: %s" % e)
        for item in L:
            try:
                _traccar_queue.put(item)
            except Exception as e:
                _log.error("traccar backup put back error: %s" % e)


# ------------------------- 消费者线程：只读队列，RETRY 时改 next_ts 后写回队列 -------------------------
def _consumer_loop():
    """消费者：只从队列取；发送成功则结束；RETRY 则改 next_ts 后 put 回队列。不操作持久化文件。"""
    global _traccar_queue, _traccar_consumer_params
    if _traccar_queue is None or _traccar_consumer_params is None:
        return
    traccar_host, traccar_port, device_id, traccar_http_timeout, traccar_max_backoff = _traccar_consumer_params
    now = utime.time
    while True:
        item = None
        try:
            if not _traccar_queue.empty():
                item = _traccar_queue.get()
        except Exception:
            pass
        if item is None:
            utime.sleep(1)
            continue

        next_ts = float(item.get("next_ts", 0))
        if next_ts > 0 and now() < next_ts:
            try:
                _traccar_queue.put(item)
            except Exception:
                pass
            utime.sleep(1)
            continue

        payload = item.get("payload", {})
        attempts = item.get("attempts", 0)
        r = send_position(traccar_host, traccar_port, device_id, payload, traccar_http_timeout)
        if r == SEND_OK:
            try:
                lat, lon = payload.get("lat"), payload.get("lon")
                msg = "Traccar Sent %.6f %.6f" % (float(lat or 0), float(lon or 0))
            except (TypeError, ValueError):
                msg = "Traccar Sent (ok)"
            _log.info(msg)
        elif r == SEND_RETRY:
            attempts += 1
            backoff = min(traccar_max_backoff, attempts * RETRY_BACKOFF_BASE_SEC)
            item["attempts"] = attempts
            item["next_ts"] = now() + backoff
            try:
                _traccar_queue.put(item)
            except Exception as e:
                _log.error("traccar retry put back error: %s" % e)
            _log.warning("Traccar retry later, backoff %s" % backoff)
        utime.sleep(0)


def start_consumer(traccar_cfg, device_id):
    """
    启动 Traccar 消费者线程与备份线程。传入 load_config() 的返回值；主程序之后只调用 enqueue。
    启动时从持久化文件加载到队列（若存在），再启动消费者与备份者。
    """
    global _traccar_queue, _traccar_consumer_params
    if Queue is None:
        _log.warning("traccar_report: Queue not available, enqueue will no-op")
        return
    traccar_host = (traccar_cfg.get("traccar_host") or "").strip()
    traccar_port = traccar_cfg.get("traccar_port", 5055)
    traccar_http_timeout = 10
    traccar_max_backoff = traccar_cfg.get("traccar_max_backoff", 60)

    _traccar_queue = Queue(200)
    _traccar_consumer_params = (traccar_host, traccar_port, device_id, traccar_http_timeout, traccar_max_backoff)

    # 启动时从持久化文件加载到队列
    if _cache_exists(TRACCAR_CACHE_FILE):
        try:
            with open(TRACCAR_CACHE_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = ujson.loads(line)
                        _traccar_queue.put(item)
                    except Exception as e:
                        _log.warning("traccar load cache line error: %s" % e)
        except Exception as e:
            _log.warning("traccar load cache error: %s" % e)

    try:
        _thread.start_new_thread(_consumer_loop, ())
        _log.info("Traccar consumer thread started")
    except Exception as e:
        _log.error("Traccar start_consumer error: %s" % e)

    try:
        _thread.start_new_thread(_backup_loop, ())
        _log.info("Traccar backup thread started")
    except Exception as e:
        _log.error("Traccar start_backup error: %s" % e)


def enqueue(payload):
    """
    生产：仅将一条位置入队。主程序只负责按间隔调用此接口打点；不写持久化文件。
    """
    global _traccar_queue, _traccar_consumer_params
    if _traccar_queue is None or _traccar_consumer_params is None:
        return
    item = {"payload": payload, "attempts": 0, "next_ts": 0}
    try:
        _traccar_queue.put(item)
    except Exception as e:
        _log.error("traccar enqueue put error: %s" % e)
    lat = payload.get("lat")
    lon = payload.get("lon")
    if lat is not None and lon is not None:
        _log.debug("Traccar Cached: %.6f %.6f" % (float(lat), float(lon)))
