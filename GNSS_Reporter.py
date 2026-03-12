# GNSS_Reporter.py - 移远 EC800M QuecPython 定位上报主循环与逻辑
#
# 功能：优先 GNSS 定位；无 GNSS 时用 LBS 基站定位；按运动/静止策略打点并入队 Traccar/APRS。
#       Traccar、APRS 采用生产-消费异步上报（Queue + 文件持久化），主循环只负责按间隔记录点位，不阻塞网络。
#       配置从 config.cfg 读取，设备 ID 使用 IMEI；刷机引脚未悬空时退出。
#       LBS 定位时按静止间隔上报。APRS 在 aprs_report.py，Traccar 在 traccar_report.py。

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

import utime
import uos
import net
import modem
import quecgnss
import usocket as socket
import dataCall
import checkNet
from machine import Pin, WDT
from misc import PowerKey,Power

try:
    import battery
except Exception as e:
    print("battery import failed:", e)
    battery = None

try:
    import cell_info
except Exception as e:
    print("cell_info import failed:", e)
    cell_info = None

try:
    import cellLocator
except Exception as e:
    print("cellLocator import failed:", e)
    cellLocator = None

try:
    import aprs_report
except Exception as e:
    print("aprs_report import failed:", e)
    aprs_report = None

try:
    import traccar_report
except Exception as e:
    print("traccar_report import failed:", e)
    traccar_report = None

try:
    import config
except Exception as e:
    print("config import failed:", e)
    config = None

try:
    import oled_display
except Exception as e:
    print("oled_display import failed:", e)

    class _OledStub:
        """无 oled_display 模块时使用，仅保证调用不报错；init_oled 返回 None 表示无屏。"""
        def init_oled(self):
            return None
        def __getattr__(self, _name):
            def _noop(*args, **kwargs):
                pass
            return _noop
    oled_display = _OledStub()

try:
    import _thread
except Exception:
    _thread = None

# ------------------------- 默认常量 -------------------------
CID = 1
PROFILE = 0
FLASH_CHECK_INTERVAL_TICKS = 30


def load_config():
    """从 config 模块读取完整配置；若 config 不可用则返回默认 dict。"""
    if config:
        return config.load_config()
    return {
        "traccar_host": "traccar.example.com",
        "traccar_port": 5055,
        "moving_interval": 10,
        "still_interval": 300,
        "still_speed_threshold": 5,
        "flash_gpio": -1,
        "network_check_timeout": 60,
        "wdt_period": 60,
        "lbs_server": "",
        "lbs_port": 80,
        "lbs_token": "",
        "lbs_timeout": 30,
        "lbs_profile_idx": 1,
        "lbs_interval": 60,
    }


# ------------------------- 刷机引脚 -------------------------
def create_flash_pin(gpio_num):
    if gpio_num < 0:
        return None
    try:
        return Pin(gpio_num, Pin.IN, Pin.PULL_PU, 1)
    except Exception:
        return None


def is_flash_mode(pin):
    if pin is None:
        return False
    try:
        return pin.read() == 0
    except Exception:
        return False


# ------------------------- IMEI -------------------------
def get_device_id():
    try:
        imei = modem.getDevImei()
        if imei and imei != -1:
            return str(imei)
    except Exception as e:
        print("getDevImei error:", e)
    return "EC800M"


# ------------------------- GNSS 解析 -------------------------
def safe_decode(b):
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return str(b)


def dm_to_deg(dm, hemi):
    if not dm:
        return None
    try:
        v = float(dm)
    except Exception:
        return None
    d = int(v // 100)
    m = v - d * 100
    deg = d + m / 60.0
    if hemi in ("S", "W"):
        deg = -deg
    return deg


def parse_gga(line):
    f = line.split(",")
    if len(f) < 10:
        return None
    fix = f[6] or "0"
    sats = f[7] or "0"
    hdop = f[8] or ""
    alt = f[9] or ""
    return fix, sats, hdop, alt


def parse_rmc(line):
    f = line.split(",")
    if len(f) < 10:
        return None
    status = f[2] or "V"
    lat = dm_to_deg(f[3], f[4])
    lon = dm_to_deg(f[5], f[6])
    spd_kn = f[7] or "0"
    course = f[8] or ""
    date = f[9] or ""
    time_utc = f[1] or ""
    return status, lat, lon, spd_kn, course, date, time_utc


gps_data = {
    "lat": None,
    "lon": None,
    "speed": 0,
    "track": None,
    "alt": None,
    "sats": 0,
    "hdop": None,
    "fix": "0",
    "accuracy": None,  # GNSS: 由 HDOP 推算(eph)；LBS: 接口返回米
}


def gnss_read_once():
    data = quecgnss.read(4096)
    if isinstance(data, (bytes, bytearray)):
        raw = data
    else:
        try:
            raw = data[1]
        except Exception:
            raw = b""
    if not raw:
        return
    text = safe_decode(raw)
    for line in text.split("\r\n"):
        if not line or not line.startswith("$"):
            continue
        if line.startswith("$GNGGA") or line.startswith("$GPGGA") or line.startswith("$BDGGA"):
            g = parse_gga(line)
            if g:
                fix, sats, hdop, alt = g
                gps_data["fix"] = fix
                gps_data["sats"] = int(sats) if sats else 0
                gps_data["hdop"] = hdop
                try:
                    gps_data["accuracy"] = float(hdop) * 2.5 if hdop else None
                except Exception:
                    gps_data["accuracy"] = None
                try:
                    gps_data["alt"] = float(alt) if alt else None
                except Exception:
                    gps_data["alt"] = None
        elif line.startswith("$GNRMC") or line.startswith("$GPRMC") or line.startswith("$BDRMC"):
            r = parse_rmc(line)
            if r:
                status, lat, lon, spd_kn, course, date, time_utc = r
                if status == "A" and lat is not None and lon is not None:
                    gps_data["lat"] = lat
                    gps_data["lon"] = lon
                    try:
                        gps_data["speed"] = float(spd_kn) * 1.852
                    except Exception:
                        gps_data["speed"] = 0
                    try:
                        gps_data["track"] = float(course) if course else None
                    except Exception:
                        gps_data["track"] = None


# ------------------------- LBS 基站定位（GNSS 无数据时备用）-------------------------
def get_lbs_location(cfg):
    """调用 cellLocator.getLocation，成功返回 (lat, lon, accuracy)，失败返回 (None, None, None)。
    若存在 _traccar_extra_lock 则持锁再调 LBS，与 cache 线程的 cell_info 刷新串行，避免底层冲突。全他妈的是坑"""
    global _traccar_extra_lock
    if not cellLocator:
        return None, None, None
    server = cfg.get("lbs_server", "").strip()
    token = cfg.get("lbs_token", "").strip()
    if not server or not token or len(token) != 16:
        return None, None, None
    port = int(cfg.get("lbs_port", 80))
    timeout = int(cfg.get("lbs_timeout", 30))
    profile_idx = int(cfg.get("lbs_profile_idx", 1))
    lock = _traccar_extra_lock
    if lock is not None:
        lock.acquire()
    try:
        result = cellLocator.getLocation(server, port, token, timeout, profile_idx)
    except Exception:
        return None, None, None
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
    if isinstance(result, tuple) and len(result) >= 3:
        lon, lat, accuracy = result[0], result[1], result[2]
        if (lon, lat, accuracy) != (0.0, 0.0, 0):
            return float(lat), float(lon), int(accuracy)
    return None, None, None


# ------------------------- 时间 -------------------------
def get_utc_timestamp():
    try:
        ts = utime.mktime(utime.localtime())
        return ts
    except Exception:
        return 0


# ------------------------- Traccar 载荷构造 -------------------------
# rssi/cell 低变化但耗时长，由独立线程写入全局缓存。与 LBS 共用底层 cell 接口，用锁串行化避免冲突。
TRACCAR_EXTRA_CACHE_INTERVAL_SEC = 10
_traccar_extra_cache = {}
_traccar_extra_lock = None  # 在 start_traccar_extra_cache_thread 中创建，LBS 与 cache 刷新共用此锁


def _traccar_extra_cache_loop():
    """后台线程：定期拉取 rssi/cell 写入全局 _traccar_extra_cache。刷新时持锁，与 LBS 串行。"""
    #这里有个坑，为了高密度打点，运营商信息不要作为阻塞调用，net和cell属于低频刷新信息，但是获取时间耗时，会影响主线程的正常运行
    global _traccar_extra_cache, _traccar_extra_lock

    def _do_refresh():
        out = {}
        if net:
            try:
                out["rssi"] = net.csqQueryPoll()
            except Exception:
                pass
        if cell_info:
            try:
                out["cell"] = cell_info.get_cell_info()
            except Exception:
                pass
        return out

    lock = _traccar_extra_lock
    # 非阻塞拿锁：拿不到就跳过本轮，避免阻塞 LBS；LBS 侧始终阻塞拿锁
    def _try_refresh():
        global _traccar_extra_cache
        if lock is None:
            _traccar_extra_cache = _do_refresh()
            return
        try:
            if not lock.acquire(0):
                return
        except TypeError:
            lock.acquire()
        try:
            _traccar_extra_cache = _do_refresh()
        finally:
            try:
                lock.release()
            except Exception:
                pass
    _try_refresh()
    while True:
        utime.sleep(TRACCAR_EXTRA_CACHE_INTERVAL_SEC)
        _try_refresh()


def start_traccar_extra_cache_thread():
    """启动 rssi/cell 缓存刷新线程；同时创建与 LBS 共用的锁，避免底层 cell 接口冲突。"""
    global _traccar_extra_lock
    if _thread is None:
        return
    try:
        _traccar_extra_lock = _thread.allocate_lock()
        _thread.start_new_thread(_traccar_extra_cache_loop, ())
        print("Traccar extra cache thread started")
    except Exception as e:
        print("Traccar extra cache thread start error:", e)


def build_traccar_payload(device_id, lat, lon, gps_data):
    """根据 gps_data 构造 Traccar 单条位置 payload。rssi/cell/battery 从全局缓存读，保证 GPS 相关数据最高刷新频率。"""
    global _traccar_extra_cache
    payload = {
        "id": device_id,
        "lat": "%.7f" % lat,
        "lon": "%.7f" % lon,
        "timestamp": get_utc_timestamp(),
    }
    speed = gps_data.get("speed")
    if speed is not None:
        payload["speed"] = "%.2f" % (float(speed) / 1.852)
    track = gps_data.get("track")
    if track is not None:
        payload["bearing"] = "%.1f" % float(track)
    alt = gps_data.get("alt")
    if alt is not None:
        payload["altitude"] = "%.1f" % float(alt)
    sats = gps_data.get("sats")
    if sats is not None:
        payload["sat"] = sats
    acc = gps_data.get("accuracy")
    if acc is not None:
        payload["accuracy"] = "%.1f" % float(acc)
    if battery:
        try:
            level, voltage = battery.get_battery()
            if level is not None:
                payload["batteryLevel"] = "%.1f" % level
            if voltage is not None:
                payload["batteryVoltage"] = voltage
        except Exception:
            pass
    if "rssi" in _traccar_extra_cache:
        payload["rssi"] = _traccar_extra_cache["rssi"]
    if "cell" in _traccar_extra_cache:
        payload["cell"] = _traccar_extra_cache["cell"]
    return payload


# PowerKey：长按>=3秒退出，短按切换显示界面
_powerkey_exit_requested = False
_powerkey_press_ts = None
_display_mode = 0  # 0=GNSS INFO, 1=Report Status, 2=精度/航向/卫星
LONG_PRESS_MS = 3000
SHORT_PRESS_MIN_MS = 50  # 防抖，过短视为误触


def _powerkey_callback(status):
    """PowerKey 回调：status 0=松开，1=按下。长按>=3s 请求退出，短按切换显示模式。"""
    global _powerkey_exit_requested, _powerkey_press_ts, _display_mode
    if status == 1:
        _powerkey_press_ts = utime.ticks_ms()
    elif status == 0 and _powerkey_press_ts is not None:
        duration = utime.ticks_diff(utime.ticks_ms(), _powerkey_press_ts)
        if duration >= LONG_PRESS_MS:
            _powerkey_exit_requested = True
        elif duration >= SHORT_PRESS_MIN_MS:
            _display_mode = (_display_mode + 1) % 3
        _powerkey_press_ts = None

# 需要重启时抛出此异常（MicroPython 中 SystemExit 可能被运行时直接处理，无法在入口处捕获）
class NeedRestart(Exception):
    pass

# ------------------------- 主流程 -------------------------
def main():
    global _powerkey_exit_requested, _display_mode
    _powerkey_exit_requested = False
    _display_mode = 0
    print("GNSS_Reporter starting...")

    # 第一时间初始化 OLED 并显示 Booting（无屏或异常时 oled_display 内部静默）
    oled_i2c = oled_display.init_oled()
    oled_display.show_boot_message(oled_i2c, "Booting...")

    def oled_status(msg):
        """将状态/报错同步到 OLED 单行（无上位机时便于看运行状态）。"""
        oled_display.show_boot_message(oled_i2c, str(msg)[:21])

    if oled_i2c is not None:
        print("OLED init ok")

    wdt = None  # 供 finally 清理用，初始化阶段 raise 时尚未创建
    try:
        cfg = load_config()
        print("config:", cfg)

        flash_pin = create_flash_pin(cfg["flash_gpio"])
        if is_flash_mode(flash_pin):
            print("Flash pin asserted, exit for flash mode.")
            oled_status("Flash mode exit")
            raise SystemExit

        device_id = get_device_id()
        print("device_id:", device_id)
        oled_status("IMEI:****" + str(device_id)[-6:])

        print("wait network...")
        oled_status("wait network...")
        stagecode, subcode = checkNet.waitNetworkReady(cfg["network_check_timeout"])
        if stagecode != 3:
            print("network not ready, exit."+str(stagecode) + "," + str(subcode))
            oled_status("net not ready")
            oled_status("net code:" + str(stagecode) + "," + str(subcode))
            raise NeedRestart("network not ready")
        print("network ready")
        oled_status("network ready")

        try:
            dataCall.getInfo(CID, PROFILE)
            utime.sleep(1)
        except Exception as e:
            print("dataCall.getInfo:", e)
            oled_status("dataCall err")

        try:
            quecgnss.configSet(0, 1)
            quecgnss.configSet(2, 1)
            quecgnss.configSet(4, 1)
        except Exception as e:
            print("quecgnss configSet:", e)
        ret = quecgnss.init()
        if ret != 0:
            print("GNSS init failed, ret:", ret)
            oled_status("GNSS init failed")
            raise NeedRestart("network not ready")
        print("GNSS init ok")
        oled_status("GNSS init ok")

        moving_interval = cfg["moving_interval"]
        still_interval = cfg["still_interval"]
        still_speed_threshold = cfg["still_speed_threshold"]

        traccar_cfg = traccar_report.load_config() if traccar_report else {}
        if traccar_report and (traccar_cfg.get("traccar_host") or "").strip():
            traccar_report.start_consumer(traccar_cfg, device_id)
            start_traccar_extra_cache_thread()

        aprs_cfg = aprs_report.load_config() if aprs_report else {}
        last_aprs_ts = 0
        if aprs_report and aprs_cfg.get("aprs_callsign"):
            aprs_report.start_consumer(aprs_cfg)

        if cfg["wdt_period"] > 0:
            try:
                wdt = WDT(cfg["wdt_period"])
                print("WDT started, period %d s" % cfg["wdt_period"])
                oled_status("WDT %ds" % cfg["wdt_period"])
            except Exception as e:
                print("WDT init failed:", e)
                oled_status("WDT init fail")

        last_report_ts = 0
        last_still_report_ts = 0
        last_lbs_ts = 0
        tick = 0

        try:
            pk = PowerKey()
            if pk.powerKeyEventRegister(_powerkey_callback) == 0:
                print("PowerKey: long press >= 3s exit, short press switch display.")
                oled_status("PowerKey Register ok")
            else:
                print("PowerKey register failed.")
                oled_status("PowerKey Register fail")
        except Exception as e:
            print("PowerKey init error:", e)
            oled_status("PowerKey Init err")
        oled_display.clear(oled_i2c)
        try:
            while True:
                try:
                    now = utime.time()
                except Exception:
                    now = 0
                if wdt:
                    try:
                        wdt.feed()
                    except Exception:
                        pass
                try:
                    tick += 1
                    if _powerkey_exit_requested:
                        print("PowerKey long press, exit.")
                        oled_status("PowerKey exit")
                        break
                    if tick % FLASH_CHECK_INTERVAL_TICKS == 0 and is_flash_mode(flash_pin):
                        print("Flash pin asserted, exit.")
                        oled_status("Flash mode exit")
                        break

                    # 1) 优先 GNSS；无 GNSS 时尽快 LBS 一次，之后按 lbs_interval 刷新，直到 GNSS 恢复
                    gnss_read_once()
                    lat = gps_data.get("lat")
                    lon = gps_data.get("lon")
                    lbs_interval = cfg.get("lbs_interval", 60)
                    no_gnss = lat is None or lon is None or gps_data.get("fix") == "0"
                    if no_gnss and cfg.get("lbs_token") and cellLocator:
                        # 首次（last_lbs_ts==0）或间隔已到：立即试 LBS。启动时 utime.time() 可能很小，仅靠 now>=lbs_interval 会一直不成立
                        if last_lbs_ts == 0 or (now - last_lbs_ts) >= lbs_interval:
                            lbs_lat, lbs_lon, lbs_acc = get_lbs_location(cfg)
                            last_lbs_ts = now
                            if lbs_lat is not None and lbs_lon is not None:
                                gps_data["lat"], gps_data["lon"] = lbs_lat, lbs_lon
                                gps_data["speed"] = 0
                                gps_data["accuracy"] = lbs_acc
                                gps_data["_source"] = "LBS"
                                lat, lon = lbs_lat, lbs_lon
                    if lat is not None and lon is not None and gps_data.get("_source") != "LBS":
                        gps_data["_source"] = "GNSS"
                    # OLED 三款界面统一刷新（电池、速度不变；短按电源键切换界面）
                    if lat is not None and lon is not None:
                        lat_disp = "N%.5f" % lat if lat >= 0 else "S%.5f" % (-lat)
                        lon_disp = "E%.5f" % lon if lon >= 0 else "W%.5f" % (-lon)
                    else:
                        lat_disp = "---"
                        lon_disp = "---"
                    gnss_type = gps_data.get("_source") or "---"
                    speed_kmh = gps_data.get("speed") or 0
                    bat_pct = None
                    if battery:
                        try:
                            bat_pct, _ = battery.get_battery()
                        except Exception:
                            pass
                    try:
                        loc = utime.localtime(now)
                        system_time_str = "%02d:%02d:%02d" % (loc[3], loc[4], loc[5])
                    except Exception:
                        system_time_str = "--:--:--"
                    aprs_ago_sec = (now - last_aprs_ts) if last_aprs_ts else None
                    # 静止时按静止间隔显示“距上次上报”，运动时按运动间隔
                    if speed_kmh <= still_speed_threshold:
                        traccar_ago_sec = (now - last_still_report_ts) if last_still_report_ts else None
                    else:
                        traccar_ago_sec = (now - last_report_ts) if last_report_ts else None
                    oled_display.update_display(
                        oled_i2c,
                        _display_mode,
                        speed_kmh,
                        bat_pct=bat_pct,
                        lat_disp=lat_disp,
                        lon_disp=lon_disp,
                        gnss_type=gnss_type,
                        aprs_ago_sec=aprs_ago_sec,
                        traccar_ago_sec=traccar_ago_sec,
                        system_time_str=system_time_str,
                        accuracy_m=gps_data.get("accuracy"),
                        heading=gps_data.get("track"),
                        sats=gps_data.get("sats"),
                    )
                    if lat is None or lon is None:
                        utime.sleep(1)
                        continue

                    # APRS：有位置且间隔到时则入队（异步上报在 aprs_report.py）
                    if aprs_report and aprs_cfg.get("aprs_callsign"):
                        aprs_interval = aprs_cfg.get("aprs_interval", 60)
                        if (now - last_aprs_ts) >= aprs_interval:
                            aprs_report.enqueue(gps_data)
                            last_aprs_ts = now

                    # 2) 间隔：速度≤阈值按静止间隔，否则按运动间隔
                    if now - last_report_ts < moving_interval:
                        utime.sleep(1)
                        continue
                    last_report_ts = now
                    if speed_kmh <= still_speed_threshold and (now - last_still_report_ts) < still_interval:
                        utime.sleep(1)
                        continue
                    last_still_report_ts = now

                    # 打点：构造 Traccar 载荷并入队（rssi/cell/battery 从全局缓存读，不阻塞）
                    payload = build_traccar_payload(device_id, lat, lon, gps_data)
                    if traccar_report:
                        traccar_report.enqueue(payload)

                    utime.sleep(1)
                except Exception as loop_err:
                    print("main_loop error:", loop_err)
                    oled_status("err:" + str(loop_err)[:17])
                    utime.sleep(2)
        finally:
            oled_status("exit.")
            oled_display.clear(oled_i2c)
            if wdt:
                try:
                    wdt.stop()
                except Exception:
                    pass
            try:
                quecgnss.gnssEnable(0)
            except Exception:
                pass
            print("GNSS_Reporter exit.")
            if _powerkey_exit_requested:
                Power.powerDown()
    except NeedRestart as e:
        print("NeedRestart:", e)
        oled_status("PowerRestarting...")
        Power.powerRestart()
    except Exception as e:
        print("Exception:", e)
        oled_status("Exception:" + str(e)[:17])
    finally:
        oled_status("exit.")
        oled_display.clear(oled_i2c)
        if wdt:
            try:
                wdt.stop()
            except Exception:
                pass
        try:
            quecgnss.gnssEnable(0)
        except Exception:
            pass
        print("GNSS_Reporter exit.")
        if _powerkey_exit_requested:
            Power.powerDown()


if __name__ == "__main__":
    main()
