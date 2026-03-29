# config.py - 统一配置文件读取（Traccar / APRS / LBS 等）
#
# 从 config.cfg 读取 key=value，供 GNSS_Reporter 与 aprs_report 使用，避免重复读文件与解析。

# 设备上合法路径固定为 /usr/config.cfg（QuecPython 无 writelines，用 write 拼接）
CONFIG_PATH = "/usr/config.cfg"
CONFIG_PATHS = (CONFIG_PATH,)

try:
    import log
    _log = log.getLogger("Config")
except Exception:
    _log = None

APRS_MIN_INTERVAL = 30

# 熄屏状态仅内存，不写文件，重启后恢复默认（屏亮）
_screen_on_remote = 1  # 1=亮 0=熄


def get_screen_on_remote():
    """远程 SCREEN ON/OFF 状态，仅内存。1=亮 0=熄，重启后为 1。"""
    return _screen_on_remote


def set_screen_on_remote(on):
    """设置远程熄屏状态（仅内存，不持久化）。on=True 或 1 为亮，否则为熄。"""
    global _screen_on_remote
    _screen_on_remote = 1 if on else 0


def get_config_path():
    """返回当前使用的配置文件路径（固定 /usr/config.cfg）。"""
    return CONFIG_PATH


def _int_val(v, default):
    """安全转 int，失败返回 default。"""
    try:
        return int(v)
    except Exception:
        return default


def _read_raw():
    """从 CONFIG_PATHS 中第一个存在的路径读取，返回 str->str 的 dict。"""
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
    return cfg


def load_config():
    """
    读取完整配置（Traccar、LBS、APRS 等），类型已转换。
    GNSS_Reporter 使用全部键；aprs_report.load_config() 可基于此返回 APRS 子集。
    """
    cfg = _read_raw()
    raw_aprs_interval = _int_val(cfg.get("aprs_interval"), 60)
    aprs_interval = max(APRS_MIN_INTERVAL, raw_aprs_interval)

    return {
        # Traccar
        "traccar_host": cfg.get("traccar_host", "traccar.example.com"),
        "traccar_port": _int_val(cfg.get("traccar_port"), 5055),
        "traccar_http_timeout": _int_val(cfg.get("http_timeout"), 10),
        "traccar_max_backoff": _int_val(cfg.get("max_backoff"), 60),
        "moving_interval": _int_val(cfg.get("moving_interval"), 10),
        "still_interval": _int_val(cfg.get("still_interval"), 300),
        "still_speed_threshold": _int_val(cfg.get("still_speed_threshold"), 5),
        "distance_threshold": _int_val(cfg.get("distance_threshold"), 0),
        "flash_gpio": _int_val(cfg.get("flash_gpio"), -1),
        "network_check_timeout": _int_val(cfg.get("network_check_timeout"), 60),
        "wdt_period": _int_val(cfg.get("wdt_period"), 60),
        "brightness": max(1, min(100, _int_val(cfg.get("brightness"), 100) or 100)),
        # LBS
        "lbs_server": cfg.get("lbs_server", "").strip(),
        "lbs_port": _int_val(cfg.get("lbs_port"), 80),
        "lbs_token": cfg.get("lbs_token", "").strip(),
        "lbs_timeout": max(1, min(300, _int_val(cfg.get("lbs_timeout"), 30))),
        "lbs_profile_idx": max(1, min(3, _int_val(cfg.get("lbs_profile_idx"), 1))),
        "lbs_interval": max(10, _int_val(cfg.get("lbs_interval"), 60)),
        # APRS
        "aprs_callsign": cfg.get("aprs_callsign", "").strip(),
        "aprs_ssid": cfg.get("aprs_ssid", "").strip(),
        "aprs_passcode": cfg.get("aprs_passcode", ""),
        "aprs_host": cfg.get("aprs_host", "rotate.aprs.net"),
        "aprs_port": _int_val(cfg.get("aprs_port"), 14580),
        "aprs_interval": aprs_interval,
        "aprs_message": cfg.get("aprs_message", "").strip(),
        "aprs_icon": (cfg.get("aprs_icon", ">") or ">")[:1],
        # 非 0：电源键动作后到 OLED 刷新返回的主循环分段耗时（GNSS_Reporter 日志 PKchain）
        "powerkey_chain_debug": _int_val(cfg.get("powerkey_chain_debug"), 0),
    }


def get_raw_value(key):
    """读取配置项原始字符串值，不存在返回 None。"""
    cfg = _read_raw()
    return cfg.get(key) if key in cfg else None


def get_all_raw():
    """读取全部 key=value 为字典（供远程 GET ALL 使用）。"""
    return _read_raw()


def set_raw_key(key, value):
    """设置或新增配置项。写入到 get_config_path() 对应文件。返回 True 成功，False 失败。"""
    path = get_config_path()
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        lines = []
    key = key.strip()
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        if "=" in line:
            k, rest = line.split("=", 1)
        else:
            k, rest = line, ""
        if k.strip() == key:
            new_lines.append(k + "=" + value + "\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(key + "=" + value + "\n")
    try:
        with open(CONFIG_PATH, "w") as f:
            f.write("".join(new_lines))
        return True
    except Exception as e:
        if _log:
            _log.warning("config set_raw_key write %s: %s" % (CONFIG_PATH, e))
        return False


def del_raw_key(key):
    """删除配置项。禁止删除 traccar_host、traccar_port。返回 (True, 被删掉的值) 或 (False, None)。"""
    key = (key or "").strip()
    if key in ("traccar_host", "traccar_port"):
        return False, None
    path = get_config_path()
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return False, None
    removed = None
    new_lines = []
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        if "=" in line:
            k, rest = line.split("=", 1)
        else:
            k, rest = line, ""
        if k.strip() == key:
            removed = rest.strip()
            continue
        new_lines.append(line)
    if removed is None:
        return False, None
    try:
        with open(CONFIG_PATH, "w") as f:
            f.write("".join(new_lines))
        return True, removed
    except Exception as e:
        if _log:
            _log.warning("config del_raw_key write %s: %s" % (CONFIG_PATH, e))
        return False, None
