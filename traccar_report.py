# traccar_report.py - Traccar 位置上报（生产-消费异步 + 持久化）
#
# 主程序只调用 enqueue(payload) 打点；本模块内维护 Queue + 文件缓存，消费者线程异步上报。
# 弱网时发送失败写入文件，重启后从文件加载到队列继续发送。

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

RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)
SEND_OK = True
SEND_RETRY = "retry"
RETRY_BACKOFF_BASE_SEC = 5

# 模块内状态：由 start_consumer 初始化
_traccar_queue = None
_traccar_file_lock = None
_traccar_consumer_params = None  # (traccar_host, traccar_port, device_id, traccar_cache_file, traccar_http_timeout, traccar_max_backoff)


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
        print("getaddrinfo error:", e)
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
                print("send_position error (retry):", e2)
                return SEND_RETRY
        else:
            print("send_position error:", e)
            return SEND_RETRY
    except Exception as e:
        print("send_position error:", e)
        return SEND_RETRY

    if resp is not None:
        head = resp[:12].decode("utf-8", "ignore")
        if "200" in head or "204" in head:
            return SEND_OK
        if any(str(c) in head for c in RETRYABLE_HTTP):
            return SEND_RETRY
    return False


# ------------------------- 持久化缓存（内部，需持锁调用）-------------------------
def _cache_exists(cache_path):
    try:
        import uos
        uos.stat(cache_path)
        return True
    except Exception:
        return False


def _cache_ensure_file(cache_path):
    if _cache_exists(cache_path):
        return
    try:
        with open(cache_path, "w") as f:
            pass
    except Exception as e:
        print("traccar_cache ensure error:", e)


def _cache_push_locked(cache_path, item):
    try:
        with open(cache_path, "a") as f:
            f.write(ujson.dumps(item) + "\n")
    except Exception as e:
        print("traccar_cache push error:", e)


def _cache_pop_locked(cache_path):
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
        print("traccar_cache pop error:", e)
    return None


def _cache_peek_next_ts_locked(cache_path):
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


def _consumer_loop():
    """消费者线程：优先从队列取，空则从文件取到期项；发送，失败则写回文件带退避。"""
    global _traccar_queue, _traccar_file_lock, _traccar_consumer_params
    if _traccar_queue is None or _traccar_file_lock is None or _traccar_consumer_params is None:
        return
    traccar_host, traccar_port, device_id, traccar_cache_file, traccar_http_timeout, traccar_max_backoff = _traccar_consumer_params
    now = utime.time
    while True:
        item = None
        from_queue = False
        if not _traccar_queue.empty():
            try:
                item = _traccar_queue.get()
                from_queue = True
            except Exception:
                pass
        if item is None:
            _traccar_file_lock.acquire()
            try:
                next_ts = _cache_peek_next_ts_locked(traccar_cache_file)
                if next_ts <= now():
                    item = _cache_pop_locked(traccar_cache_file)
            except Exception:
                pass
            try:
                _traccar_file_lock.release()
            except Exception:
                pass
        if item is None:
            utime.sleep(1)
            continue

        payload = item.get("payload", {})
        attempts = item.get("attempts", 0)
        r = send_position(traccar_host, traccar_port, device_id, payload, traccar_http_timeout)
        if r == SEND_OK:
            if from_queue:
                _traccar_file_lock.acquire()
                try:
                    _cache_pop_locked(traccar_cache_file)
                except Exception:
                    pass
                try:
                    _traccar_file_lock.release()
                except Exception:
                    pass
            print("Traccar Sent: %s %s" % (
                "%.6f" % float(payload.get("lat", 0)),
                "%.6f" % float(payload.get("lon", 0)),
            ))
        elif r == SEND_RETRY:
            attempts += 1
            backoff = min(traccar_max_backoff, attempts * RETRY_BACKOFF_BASE_SEC)
            item["attempts"] = attempts
            item["next_ts"] = now() + backoff
            _traccar_file_lock.acquire()
            try:
                _cache_push_locked(traccar_cache_file, item)
            finally:
                try:
                    _traccar_file_lock.release()
                except Exception:
                    pass
            print("Traccar retry later, backoff", backoff)
        utime.sleep(0)


def start_consumer(traccar_cfg, device_id):
    """
    启动 Traccar 消费者线程。传入 load_config() 的返回值；主程序之后只调用 enqueue。
    """
    global _traccar_queue, _traccar_file_lock, _traccar_consumer_params
    if Queue is None:
        print("traccar_report: Queue not available, enqueue will no-op")
        return
    traccar_host = (traccar_cfg.get("traccar_host") or "").strip()
    traccar_port = traccar_cfg.get("traccar_port", 5055)
    traccar_cache_file = "/usr/traccar_cache.txt"
    traccar_http_timeout = 10
    traccar_max_backoff = traccar_cfg.get("traccar_max_backoff", 60)

    _traccar_file_lock = _thread.allocate_lock()
    _traccar_queue = Queue(200)
    _traccar_consumer_params = (traccar_host, traccar_port, device_id, traccar_cache_file, traccar_http_timeout, traccar_max_backoff)
    _cache_ensure_file(traccar_cache_file)

    _traccar_file_lock.acquire()
    try:
        while _cache_exists(traccar_cache_file):
            next_ts = _cache_peek_next_ts_locked(traccar_cache_file)
            if next_ts > utime.time():
                break
            item = _cache_pop_locked(traccar_cache_file)
            if item is None:
                break
            try:
                if not _traccar_queue.put(item):
                    _cache_push_locked(traccar_cache_file, item)
                    break
            except Exception:
                _cache_push_locked(traccar_cache_file, item)
                break
    except Exception as e:
        print("traccar load cache error:", e)
    finally:
        try:
            _traccar_file_lock.release()
        except Exception:
            pass

    try:
        _thread.start_new_thread(_consumer_loop, ())
        print("Traccar consumer thread started")
    except Exception as e:
        print("Traccar start_consumer error:", e)


def enqueue(payload):
    """
    生产：将一条位置入队并追加到持久化文件。主程序只负责按间隔调用此接口打点。
    """
    global _traccar_queue, _traccar_file_lock, _traccar_consumer_params
    if _traccar_queue is None or _traccar_consumer_params is None:
        return
    _, _, _, traccar_cache_file, _, _ = _traccar_consumer_params
    item = {"payload": payload, "attempts": 0, "next_ts": 0}
    if _traccar_file_lock is not None:
        _traccar_file_lock.acquire()
        try:
            _cache_push_locked(traccar_cache_file, item)
        finally:
            try:
                _traccar_file_lock.release()
            except Exception:
                pass
    try:
        _traccar_queue.put(item)
    except Exception as e:
        print("traccar enqueue put error:", e)
    lat = payload.get("lat")
    lon = payload.get("lon")
    if lat is not None and lon is not None:
        print("Traccar Cached: %.6f %.6f" % (float(lat), float(lon)))
