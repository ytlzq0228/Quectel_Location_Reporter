# traccar_report.py - Traccar 位置上报（生产-消费异步 + 持久化）
# 主程序只调用 enqueue(payload)。支持 Osmand 指令：cmd_osmand 延迟导入，避免加载失败拖垮本模块。
import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")
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

TRACCAR_CACHE_FILE = "/usr/traccar_cache.txt"
BACKUP_INTERVAL_SEC = 30

_traccar_queue = None
_traccar_consumer_params = None
_traccar_need_reboot = False
_cmd_osmand_module = None


def _get_cmd_osmand():
    """延迟导入 cmd_osmand，首次需要时再加载；强制 /usr 优先并清除已缓存，确保加载到带 execute 的版本。"""
    global _cmd_osmand_module
    if _cmd_osmand_module is None:
        try:
            import sys
            if "/usr" not in sys.path:
                sys.path.insert(0, "/usr")
            else:
                while "/usr" in sys.path:
                    sys.path.remove("/usr")
                sys.path.insert(0, "/usr")
            if "cmd_osmand" in sys.modules:
                del sys.modules["cmd_osmand"]
            import cmd_osmand
            if not getattr(cmd_osmand, "execute", None):
                _log.warning("Traccar: cmd_osmand loaded but no execute(), check /usr/cmd_osmand.py")
            else:
                _cmd_osmand_module = cmd_osmand
        except Exception as e:
            _log.warning("Traccar: cmd_osmand import failed: %s" % e)
    return _cmd_osmand_module


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


def _url_encode(s):
    """对 lastCmdResult 做 URL 编码（与 Lua 端一致：% & = 空格 换行）。"""
    if s is None:
        return ""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("%", "%25").replace("&", "%26").replace("=", "%3D").replace(" ", "%20").replace("\n", "%0A")
    return s


def send_cmd_result(host, port, device_id, last_cmd_result, timeout_s=10):
    """仅上报指令执行结果：GET 只带 id 与 lastCmdResult。成功返回 True，否则 False。"""
    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
    except Exception as e:
        _log.warning("send_cmd_result getaddrinfo error: %s" % e)
        return False
    qs = "id=" + str(device_id) + "&lastCmdResult=" + _url_encode(last_cmd_result)
    path = "/?" + qs
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%s\r\n"
        "User-Agent: EC800M-GNSS-Traccar\r\n"
        "Connection: close\r\n\r\n"
    ) % (path, host, port)
    try:
        s = socket.socket()
        s.settimeout(timeout_s)
        s.connect(addr)
        s.send(req.encode())
        resp = b""
        while True:
            try:
                buf = s.recv(256)
            except OSError as e:
                if e.args and e.args[0] == 107 and len(resp) > 0:
                    break
                raise
            if not buf:
                break
            resp += buf
        s.close()
    except Exception as e:
        _log.warning("Report cmd result failed: %s" % e)
        return False
    head = resp[:12].decode("utf-8", "ignore") if resp else ""
    if "200" in head or "204" in head:
        _log.info("Traccar Cmd result reported: %s" % (str(last_cmd_result)[:80]))
        return True
    _log.warning("Report cmd result failed: %s" % head)
    return False


def _parse_http_response(resp):
    """解析 HTTP 响应，返回 (status_code, body_str)。body 为 None 表示解析失败或无 body。"""
    if not resp:
        return None, None
    try:
        text = resp.decode("utf-8", "ignore")
    except Exception:
        return None, None
    idx = text.find("\r\n\r\n")
    if idx < 0:
        idx = text.find("\n\n")
    if idx < 0:
        return None, None
    head_block = text[:idx]
    body = text[idx + 4:] if idx + 4 < len(text) else ""
    status = None
    for line in head_block.split("\n"):
        line = line.strip()
        if line.upper().startswith("HTTP/"):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                try:
                    status = int(parts[1])
                except ValueError:
                    pass
            break
    return status, body.strip() if body else ""


def send_position(host, port, device_id, payload, timeout_s=10):
    """用 GET 上报位置；若响应 body 非空则执行 Osmand 指令并上报 lastCmdResult。只返回 SEND_OK / SEND_RETRY / False，需重启时写全局 _traccar_need_reboot。"""
    global _traccar_need_reboot
    _traccar_need_reboot = False
    try:
        addr = socket.getaddrinfo(host, port)[0][-1]
    except Exception as e:
        _log.error("Traccar 发送失败(不可重试), 原因: getaddrinfo error: %s" % e)
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
                _log.warning("Traccar 发送失败(将重试), 原因: %s" % e2)
                return SEND_RETRY
        else:
            _log.error("Traccar 发送失败(将重试), 原因: %s" % e)
            return SEND_RETRY
    except Exception as e:
        _log.error("Traccar 发送失败(将重试), 原因: %s" % e)
        return SEND_RETRY

    if resp is None:
        _log.warning("Traccar 发送失败(将重试), 原因: no response from server")
        return SEND_RETRY

    status, body = _parse_http_response(resp)
    url_short = (path[:120] + "...") if len(path) > 120 else path
    body_len = len(body) if body else 0

    cmd_mod = _get_cmd_osmand()
    execute_fn = getattr(cmd_mod, "execute", None) if cmd_mod else None
    if body and body != "" and cmd_mod and callable(execute_fn):
        # 仅在有指令时打印发送与响应日志
        #_log.info("Traccar [REQ] GET %s" % url_short)
        _log.info("Traccar [RESP] code=%s body(len)=%s" % (status, body_len))
        try:
            last_result, need_reboot = execute_fn(body)
        except Exception as e:
            _log.warning("Traccar [CMD] execute error: %s" % e)
            last_result, need_reboot = "EXCEPTION: %s" % str(e)[:60], False
        _traccar_need_reboot = need_reboot
        _log.info("Traccar [CMD] LastCmdResult=%s RebootRequest=%s" % (last_result, need_reboot))
        send_cmd_result(host, port, device_id, last_result, timeout_s)
        if status is None or status < 0:
            _log.error("Traccar 发送失败(将重试), 原因: no valid HTTP response")
            return SEND_RETRY
        if status == 200 or status == 204:
            return SEND_OK
        if status in RETRYABLE_HTTP:
            _log.warning("Traccar 发送失败(将重试), 原因: HTTP %s" % (status or "?"))
            return SEND_RETRY
        _log.warning("Traccar 发送失败(将重试), 原因: HTTP %s" % (status or "?"))
        return SEND_RETRY
    elif body and body != "":
        _log.warning("Traccar [RESP] body 有内容但未执行(无 cmd_osmand.execute), raw=%s" % (body[:128] if len(body) > 128 else body))

    if status is None or status < 0:
        _log.error("Traccar 发送失败(将重试), 原因: no valid HTTP response")
        return SEND_RETRY
    if status == 200 or status == 204:
        return SEND_OK
    if status in RETRYABLE_HTTP:
        _log.warning("Traccar 发送失败(将重试), 原因: HTTP %s" % status)
        return SEND_RETRY
    resp_preview = resp[:120].decode("utf-8", "ignore")
    if len(resp) > 120:
        resp_preview = resp_preview + "..."
    _log.error("Traccar 发送失败(不可重试), 原因: HTTP %s %s" % (status or "?", resp_preview))
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


def _do_reboot():
    """重启设备，与 GNSS_Reporter、fota_update 一致：misc.Power.powerRestart()。"""
    try:
        import misc
        misc.Power.powerRestart()
    except Exception as e:
        _log.warning("Traccar reboot failed: %s" % e)


# ------------------------- 消费者线程：只读队列，RETRY 时改 next_ts 后写回队列 -------------------------
def _consumer_loop():
    """消费者：只从队列取；发送成功则结束；RETRY 则改 next_ts 后 put 回队列。服务器下发 Osmand 指令时执行并上报，需重启时调用 _do_reboot。"""
    global _traccar_queue, _traccar_consumer_params, _traccar_need_reboot
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
        need_reboot = _traccar_need_reboot

        if r == SEND_OK:
            try:
                lat, lon = payload.get("lat"), payload.get("lon")
                msg = "Traccar Sent %.6f %.6f" % (float(lat or 0), float(lon or 0))
            except (TypeError, ValueError):
                msg = "Traccar Sent (ok)"
            _log.info(msg)
            if need_reboot:
                _log.info("Traccar Reboot requested, rebooting...")
                _do_reboot()
        elif r == SEND_RETRY:
            attempts += 1
            backoff = min(traccar_max_backoff, attempts * RETRY_BACKOFF_BASE_SEC)
            item["attempts"] = attempts
            item["next_ts"] = now() + backoff
            try:
                _traccar_queue.put(item)
            except Exception as e:
                _log.error("traccar retry put back error: %s" % e)
            _log.warning("Traccar 发送失败(将重试), backoff %ss" % backoff)
            if need_reboot:
                _log.info("Traccar Reboot requested, rebooting...")
                _do_reboot()
        else:
            _log.error("Traccar 发送失败(不可重试)")
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
        return
