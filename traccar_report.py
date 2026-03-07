# traccar_report.py - Traccar 位置上报能力
#
# 通过 HTTP GET 向 Traccar 服务器上报单条位置；
# 调用方负责构造 payload、设备 ID、网络与循环逻辑。

import utime
import usocket as socket

RETRYABLE_HTTP = (408, 429, 500, 502, 503, 504)


def send_position(host, port, device_id, payload, timeout_s=10):
    """用 GET 请求上报一条位置。成功返回 True，可重试错误返回 'retry'，其它失败返回 False。"""
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
                return "retry"
        else:
            print("send_position error:", e)
            return "retry"
    except Exception as e:
        print("send_position error:", e)
        return "retry"

    if resp is not None:
        head = resp[:12].decode("utf-8", "ignore")
        if "200" in head or "204" in head:
            return True
        if any(str(c) in head for c in RETRYABLE_HTTP):
            return "retry"
    return False
