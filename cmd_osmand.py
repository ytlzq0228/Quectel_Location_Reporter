# -*- coding: utf-8 -*-
# cmd_osmand.py - Osmand protocol: parse and run commands from Traccar response body.
# SET key=val, GET key, GET ALL, DEL key, REBOOT, SCREEN OFF, SCREEN ON, FOTA UPDATE.

import sys
import utime
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

try:
    import config as shared_config
except Exception:
    shared_config = None

try:
    import log
    _log = log.getLogger("CmdOsmand")
except Exception:
    _log = None

try:
    import _thread
except Exception:
    _thread = None

try:
    import fota_update
except Exception:
    fota_update = None


def _trim(s):
    if s is None or not isinstance(s, str):
        return ""
    return s.strip()


def parse(cmd_str):
    """
    解析单条指令（不依赖 re 模块，兼容 QuecPython）。
    返回: None 或 {"cmd": "REBOOT"} 或 {"cmd": "SET", "pairs": [...]} 或 {"cmd": "GET", "key": k} 或 {"cmd": "DEL", "key": k}
            或 {"cmd": "SCREEN_OFF"} / {"cmd": "SCREEN_ON"} / {"cmd": "FOTA_UPDATE"}
    """
    if not cmd_str or not isinstance(cmd_str, str):
        return None
    cmd_str = _trim(cmd_str)
    if cmd_str == "":
        return None

    u = cmd_str.upper()
    if u == "REBOOT":
        return {"cmd": "REBOOT"}
    if u == "SCREEN OFF":
        return {"cmd": "SCREEN_OFF"}
    if u == "SCREEN ON":
        return {"cmd": "SCREEN_ON"}
    if u == "FOTA UPDATE":
        return {"cmd": "FOTA_UPDATE"}

    if u.startswith("SET "):
        rest = _trim(cmd_str[4:])
        pairs = []
        for part in rest.split():
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = _trim(k), _trim(v)
                if k:
                    pairs.append({"key": k, "value": v})
        if pairs:
            return {"cmd": "SET", "pairs": pairs}
        if "=" in rest:
            k, v = rest.split("=", 1)
            return {"cmd": "SET", "pairs": [{"key": _trim(k), "value": _trim(v)}]}
        return None

    if u.startswith("GET "):
        k = _trim(cmd_str[4:])
        if k:
            return {"cmd": "GET", "key": k}
        return None

    if u.startswith("DEL "):
        k = _trim(cmd_str[4:])
        if k:
            return {"cmd": "DEL", "key": k}
        return None

    return None


def _execute_one(parsed):
    """执行单条已解析指令。返回 (LastCmdResult, RebootRequest)。"""
    if not parsed:
        return "UNKNOWN CMD", False

    cmd = parsed.get("cmd")
    if not shared_config:
        return "CONFIG UNAVAILABLE", False

    if cmd == "REBOOT":
        return "REBOOT OK", True

    if cmd == "SCREEN_OFF":
        if not shared_config:
            return "SCREEN OFF ERR", False
        shared_config.set_screen_on_remote(0)
        return "SCREEN OFF OK", False

    if cmd == "SCREEN_ON":
        if not shared_config:
            return "SCREEN ON ERR", False
        shared_config.set_screen_on_remote(1)
        return "SCREEN ON OK", False

    if cmd == "FOTA_UPDATE":
        if not fota_update or not _thread:
            return "FOTA UPDATE ERR", False

        def _run_fota():
            try:
                utime.sleep(3)
                fota_update.run_fota_with_progress(
                    oled_status_cb=None,
                    log_info_cb=(_log.info if _log else None),
                )
            except Exception as e:
                if _log:
                    _log.warning("FOTA thread error: %s" % e)

        try:
            _thread.start_new_thread(_run_fota, ())
            return "FOTA UPDATE OK", False
        except Exception as e:
            if _log:
                _log.warning("FOTA start thread error: %s" % e)
            return "FOTA UPDATE ERR", False

    if cmd == "SET":
        pairs = parsed.get("pairs") or []
        if not pairs:
            return "SET = ERR", False
        parts = []
        ok_all = True
        for p in pairs:
            k = (p.get("key") or "").strip()
            v = p.get("value")
            if v is None:
                v = ""
            if not k:
                ok_all = False
                parts.append("= ERR")
                break
            ok = shared_config.set_raw_key(k, v)
            if ok:
                parts.append(k + "=" + str(v))
            else:
                ok_all = False
                parts.append(k + "= ERR")
                break
        result = "SET " + " ".join(parts) + (" OK" if ok_all else "")
        return result, ok_all

    if cmd == "GET":
        key = parsed.get("key")
        if key and key.upper() == "ALL":
            cfg = shared_config.get_all_raw()
            keys = sorted(cfg.keys())
            parts = [k + "=" + str(cfg.get(k, "")) for k in keys]
            return "GET ALL " + " ".join(parts) + " OK", False
        if key:
            v = shared_config.get_raw_value(key)
            if v is not None:
                return "GET " + key + "=" + str(v) + " OK", False
            return "GET " + key + "= ERR", False
        return "GET = ERR", False

    if cmd == "DEL":
        key = (parsed.get("key") or "").strip()
        if key in ("traccar_host", "traccar_port"):
            return "DEL " + key + "= FORBIDDEN", False
        ok, removed = shared_config.del_raw_key(key)
        if ok:
            return "DEL " + key + "=" + str(removed or "") + " OK", True
        return "DEL " + key + "= ERR", False

    return "UNKNOWN CMD", False


def execute(cmd_str):
    """
    执行指令原文（可多行，只执行第一行有效指令）。
    返回 (LastCmdResult: str, RebootRequest: bool)。
    """
    if not cmd_str or not isinstance(cmd_str, str) or _trim(cmd_str) == "":
        return "EMPTY", False

    first_line = (cmd_str.split("\n")[0].split("\r")[0] or "").strip()
    if first_line == "":
        return "EMPTY", False

    parsed = parse(first_line)
    if parsed is None:
        return "UNKNOWN: " + (first_line[:64] if len(first_line) > 64 else first_line), False

    result, reboot = _execute_one(parsed)
    return result, reboot
