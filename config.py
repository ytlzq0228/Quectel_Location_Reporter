# config.py - 统一配置文件读取（Traccar / APRS / LBS 等）
#
# 从 config.cfg 读取 key=value，供 GNSS_Reporter 与 aprs_report 使用，避免重复读文件与解析。

CONFIG_PATHS = ("config.cfg", "/usr/config.cfg")

APRS_MIN_INTERVAL = 30


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
        "flash_gpio": _int_val(cfg.get("flash_gpio"), -1),
        "network_check_timeout": _int_val(cfg.get("network_check_timeout"), 60),
        "wdt_period": _int_val(cfg.get("wdt_period"), 60),
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
    }
