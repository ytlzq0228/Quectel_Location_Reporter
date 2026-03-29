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
import math
import uos
import net
import modem
import quecgnss
import usocket as socket
import dataCall
import checkNet
import ntptime
import log
from machine import Pin, WDT
from misc import PowerKey,Power

log.basicConfig(level=log.INFO)
_log = log.getLogger("GNSS_Reporter")

try:
    import battery
except Exception as e:
    _log.warning("battery import failed: %s" % e)
    battery = None

try:
    import cell_info
except Exception as e:
    _log.warning("cell_info import failed: %s" % e)
    cell_info = None

try:
    import cellLocator
except Exception as e:
    _log.warning("cellLocator import failed: %s" % e)
    cellLocator = None

try:
    import aprs_report
except Exception as e:
    _log.warning("aprs_report import failed: %s" % e)
    aprs_report = None

traccar_report_err = None
try:
    import traccar_report
except Exception as e:
    traccar_report_err = e
    traccar_report = None

try:
    import config
except Exception as e:
    _log.warning("config import failed: %s" % e)
    config = None

try:
    import fota_update
except Exception as e:
    _log.warning("fota_update import failed: %s" % e)
    fota_update = None

try:
    import oled_display
except Exception as e:
    _log.warning("oled_display import failed: %s" % e)

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
VERSION = "1.4.1"
STATUS_LED_GPIO = 44  # EC800M/EG810M: GPIO44 -> 引脚25


class StatusLed:
    """状态灯控制：boot 慢闪、run 心跳闪、error 快闪。"""
    MODE_OFF = "off"
    MODE_BOOT = "boot"
    MODE_RUN = "run"
    MODE_ERROR = "error"

    def __init__(self, gpio_num):
        self._pin = None
        self._mode = self.MODE_OFF
        self._state = 0
        self._next_toggle_ms = 0
        try:
            self._pin = Pin(gpio_num, Pin.OUT, Pin.PULL_DISABLE, 0)
        except Exception as e:
            _log.warning("STATUS LED init failed on GPIO%s: %s" % (gpio_num, e))
            self._pin = None

    def _write(self, value):
        if self._pin is None:
            return
        try:
            self._pin.write(1 if value else 0)
            self._state = 1 if value else 0
        except Exception:
            pass

    def set_mode(self, mode):
        self._mode = mode
        now = utime.ticks_ms()
        if mode == self.MODE_OFF:
            self._write(0)
            return
        if mode == self.MODE_RUN:
            # 运行态：心跳闪（短亮一次后长灭）
            self._write(1)
            self._next_toggle_ms = utime.ticks_add(now, 100)
            return
        # boot/error 都从灭灯开始，确保模式切换时节奏一致
        self._write(0)
        self._next_toggle_ms = utime.ticks_add(now, 100)

    def tick(self):
        if self._pin is None or self._mode == self.MODE_OFF:
            return
        now = utime.ticks_ms()
        if utime.ticks_diff(now, self._next_toggle_ms) < 0:
            return
        if self._mode == self.MODE_BOOT:
            # 启动态：慢闪 200ms 亮 / 800ms 灭
            next_state = 0 if self._state else 1
            self._write(next_state)
            delay = 200 if next_state else 800
            self._next_toggle_ms = utime.ticks_add(now, delay)
        elif self._mode == self.MODE_RUN:
            # 运行态：心跳 100ms 亮 / 1900ms 灭
            next_state = 0 if self._state else 1
            self._write(next_state)
            delay = 100 if next_state else 1900
            self._next_toggle_ms = utime.ticks_add(now, delay)
        elif self._mode == self.MODE_ERROR:
            # 异常态：快闪 200ms 亮 / 200ms 灭
            next_state = 0 if self._state else 1
            self._write(next_state)
            self._next_toggle_ms = utime.ticks_add(now, 200)

    def off(self):
        self.set_mode(self.MODE_OFF)


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
        _log.error("getDevImei error: %s" % e)
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
        if line.startswith("$") and len(line) >= 6 and line[3:6] == "GGA":
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
        elif line.startswith("$") and len(line) >= 6 and line[3:6] == "RMC":
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
        _log.info("Traccar extra cache thread started")
    except Exception as e:
        _log.warning("Traccar extra cache thread start error: %s" % e)


def build_traccar_payload(device_id, lat, lon, gps_data):
    """根据 gps_data 构造 Traccar 单条位置 payload。rssi/cell/battery 从全局缓存读，保证 GPS 相关数据最高刷新频率。"""
    global _traccar_extra_cache
    payload = {
        "id": device_id,
        "lat": "%.7f" % lat,
        "lon": "%.7f" % lon,
        "timestamp": get_utc_timestamp(),
        "version": VERSION,
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


# PowerKey：仅短按/长按，长按阈值 1500ms；短按轮播信息页或设置项，长按进设置或确定
_powerkey_exit_requested = False
_powerkey_fota_requested = False
_powerkey_press_ts = None
_display_mode = 0       # 0/1/2 三个信息页
_in_settings = False   # 是否在设置页
_settings_option = 0   # 设置项 0=熄屏 1=关机 2=FOTA
# config 不可用时用本地标志；正常时用 config.get/set_screen_on_remote（与远程指令一致）
_screen_off_local = False
LONG_PRESS_MS = 1500
SHORT_PRESS_MIN_MS = 50

SETTINGS_OPTIONS = ("Screen off", "Power off", "FOTA")

# powerkey_chain_debug=1 时：记录从电源键回调打点起，到本轮 OLED 刷新返回的各阶段耗时（ms，ticks_ms）
_pk_chain_dbg = False
_pk_chain_t0_ms = None
_pk_chain_id = 0
_pk_chain_reason = ""


def _pk_chain_bool(v):
    """配置里 1 / true / yes / on 均视为开启。"""
    if v is None:
        return False
    try:
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
    except Exception:
        pass
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def _pk_chain_arm(reason):
    """在电源键回调里调用：仅记时刻，不打日志（避免在中断上下文打印）。"""
    global _pk_chain_t0_ms, _pk_chain_id, _pk_chain_reason
    if not _pk_chain_dbg:
        return
    _pk_chain_t0_ms = utime.ticks_ms()
    _pk_chain_id += 1
    _pk_chain_reason = reason


def _pk_chain_tick(tag):
    global _pk_chain_t0_ms
    if _pk_chain_t0_ms is None or not _pk_chain_dbg:
        return
    dt = utime.ticks_diff(utime.ticks_ms(), _pk_chain_t0_ms)
    _log.info(
        "PKchain id=%d %-30s +%dms (since key, reason=%s)"
        % (_pk_chain_id, tag, dt, _pk_chain_reason)
    )


def _is_screen_off():
    if config is not None and getattr(config, "get_screen_on_remote", None):
        try:
            return config.get_screen_on_remote() == 0
        except Exception:
            pass
    return _screen_off_local


def _set_screen_power_off(off):
    """off=True 熄屏，False 亮屏。与 cmd_osmand 远程 SCREEN OFF/ON 共用内存状态。"""
    global _screen_off_local
    if config is not None and getattr(config, "set_screen_on_remote", None):
        try:
            config.set_screen_on_remote(0 if off else 1)
            return
        except Exception:
            pass
    _screen_off_local = off


def _powerkey_callback(status):
    """短按：信息页轮播(3 页)或设置项切换；长按：进设置或确定当前选项。熄屏后仅短按恢复。"""
    global _powerkey_exit_requested, _powerkey_fota_requested, _powerkey_press_ts
    global _display_mode, _in_settings, _settings_option
    if status == 1:
        _powerkey_press_ts = utime.ticks_ms()
    elif status == 0 and _powerkey_press_ts is not None:
        duration = utime.ticks_diff(utime.ticks_ms(), _powerkey_press_ts)
        if duration < SHORT_PRESS_MIN_MS:
            pass
        elif _is_screen_off():
            _set_screen_power_off(False)
            _pk_chain_arm("wake_screen")
        elif _in_settings:
            if duration >= LONG_PRESS_MS:
                if _settings_option == 0:
                    _set_screen_power_off(True)
                    _in_settings = False
                elif _settings_option == 1:
                    _powerkey_exit_requested = True
                else:
                    _powerkey_fota_requested = True
            else:
                _settings_option = (_settings_option + 1) % 3
                _pk_chain_arm("settings_next")
        else:
            if duration >= LONG_PRESS_MS:
                _in_settings = True
                _settings_option = 0
                _pk_chain_arm("enter_settings")
            else:
                _display_mode = (_display_mode + 1) % 3
                _pk_chain_arm("cycle_mode")
        _powerkey_press_ts = None

# 需要重启时抛出此异常（MicroPython 中 SystemExit 可能被运行时直接处理，无法在入口处捕获）
class NeedRestart(Exception):
    pass

# ------------------------- 主流程 -------------------------
def main():
    global _powerkey_exit_requested, _powerkey_fota_requested, _display_mode
    global _in_settings, _settings_option, _screen_off_local
    global _pk_chain_dbg, _pk_chain_t0_ms, _pk_chain_id, _pk_chain_reason
    _powerkey_exit_requested = False
    _powerkey_fota_requested = False
    _display_mode = 0
    _in_settings = False
    _settings_option = 0
    _screen_off_local = False
    _pk_chain_t0_ms = None
    _pk_chain_id = 0
    _pk_chain_reason = ""
    _log.info("GNSS_Reporter starting...")
    _log.info("Version: %s" % VERSION)
    status_led = StatusLed(STATUS_LED_GPIO)
    status_led.set_mode(StatusLed.MODE_BOOT)
    # 第一时间初始化 OLED 并显示 Booting（无屏或异常时 oled_display 内部静默）
    oled_i2c = oled_display.init_oled()
    oled_display.show_boot_message(oled_i2c, "Booting...")
    oled_display.show_boot_message(oled_i2c, "Version: %s" % VERSION)

    def oled_status(msg):
        """将状态/报错同步到 OLED 单行（无上位机时便于看运行状态）。"""
        oled_display.show_boot_message(oled_i2c, str(msg)[:21])

    if oled_i2c is not None:
        _log.info("OLED init ok")

    wdt = None  # 供 finally 清理用，初始化阶段 raise 时尚未创建
    shutdown_requested = False  # NeedRestart/异常等场景：清理后关机
    try:
        cfg = load_config()
        # load_config 与原始字符串双读：任一为真即开启（例如仍写 true/yes 而 int 解析为 0 时）
        _pk_chain_dbg = _pk_chain_bool(cfg.get("powerkey_chain_debug", 0))
        try:
            if config is not None and getattr(config, "get_raw_value", None):
                rv = config.get_raw_value("powerkey_chain_debug")
                if rv is not None and str(rv).strip() != "":
                    _pk_chain_dbg = _pk_chain_dbg or _pk_chain_bool(rv)
        except Exception:
            pass
        _log.info(
            "GNSS_Reporter: powerkey_chain_debug effective=%s (1=PKchain logs; set in /usr/config.cfg)"
            % (1 if _pk_chain_dbg else 0)
        )
        _log.debug("config: %s" % cfg)

        flash_pin = create_flash_pin(cfg["flash_gpio"])
        if is_flash_mode(flash_pin):
            _log.info("Flash pin asserted, exit for flash mode.")
            oled_status("Flash mode exit")
            raise SystemExit

        device_id = get_device_id()
        _log.info("device_id: %s" % device_id)
        oled_status("IMEI:****" + str(device_id)[-6:])

        _log.info("wait network...")
        oled_status("wait network...")
        stagecode, subcode = checkNet.waitNetworkReady(cfg["network_check_timeout"])
        if stagecode != 3:
            _log.error("network not ready, exit. stagecode=%s subcode=%s" % (stagecode, subcode))
            oled_status("net not ready")
            oled_status("net code:" + str(stagecode) + "," + str(subcode))
            raise NeedRestart("network not ready")
        _log.info("network ready")
        oled_status("network ready")
        status_led.set_mode(StatusLed.MODE_RUN)

        try:
            tz = utime.getTimeZone()
            ret_ntp = ntptime.settime(timezone=tz, use_rhost=1, timeout=10)
            if ret_ntp == 0:
                _log.info("NTP sync ok")
                oled_status("NTP ok")
                loc = utime.localtime()
                oled_status("%04d%02d%02d %02d:%02d" % (loc[0], loc[1], loc[2], loc[3], loc[4]))
            else:
                _log.warning("NTP sync failed, ret: %s" % ret_ntp)
                oled_status("NTP fail")
        except Exception as e:
            _log.error("NTP settime: %s" % e)
            oled_status("NTP err")

        try:
            dataCall.getInfo(CID, PROFILE)
            utime.sleep(1)
        except Exception as e:
            _log.warning("dataCall.getInfo: %s" % e)
            oled_status("dataCall err")

        try:
            quecgnss.configSet(0,1)#设置定位星系为GPS+Beidou
            quecgnss.configSet(1,3)#只开启GGA+RMC输出
            quecgnss.configSet(2,1)#打开AGPS
            quecgnss.configSet(3,1)#使能APFLASH
            quecgnss.configSet(4,1)#打开备电
        except Exception as e:
            _log.warning("quecgnss configSet: %s" % e)
        ret = quecgnss.init()
        if ret != 0:
            _log.error("GNSS init failed, ret: %s" % ret)
            oled_status("GNSS init failed")
            raise NeedRestart("GNSS init failed")
        _log.info("GNSS init ok")
        oled_status("GNSS init ok")

        moving_interval = cfg["moving_interval"]
        still_interval = cfg["still_interval"]
        still_speed_threshold = cfg["still_speed_threshold"]
        distance_threshold = cfg.get("distance_threshold", 0) or 0

        traccar_cfg = traccar_report.load_config() if traccar_report else {}
        if not traccar_report:
            err_msg = (" (%s)" % traccar_report_err) if traccar_report_err else ""
            _log.warning("Traccar disabled: module not loaded%s. Check traccar_report.py on device." % err_msg)
            if traccar_report_err and hasattr(traccar_report_err, "filename") and hasattr(traccar_report_err, "lineno"):
                _log.warning("Traccar syntax at %s line %s" % (getattr(traccar_report_err, "filename", "?"), getattr(traccar_report_err, "lineno", "?")))
        elif not (traccar_cfg.get("traccar_host") or "").strip():
            _log.info("Traccar disabled: traccar_host empty")
        else:
            traccar_report.start_consumer(traccar_cfg, device_id)
            start_traccar_extra_cache_thread()
            _log.info("Traccar enabled: %s:%s" % (traccar_cfg.get("traccar_host", ""), traccar_cfg.get("traccar_port", 5055)))

        aprs_cfg = aprs_report.load_config() if aprs_report else {}
        last_aprs_ts = 0
        if aprs_report and aprs_cfg.get("aprs_callsign"):
            aprs_report.start_consumer(aprs_cfg)

        if cfg["wdt_period"] > 0:
            try:
                wdt = WDT(cfg["wdt_period"])
                _log.info("WDT started, period %d s" % cfg["wdt_period"])
                oled_status("WDT %ds" % cfg["wdt_period"])
            except Exception as e:
                _log.warning("WDT init failed: %s" % e)
                oled_status("WDT init fail")

        last_report_ts = 0
        last_still_report_ts = 0
        last_lbs_ts = 0
        last_report_lat = None
        last_report_lon = None
        tick = 0
        prev_in_settings = False
        prev_settings_option = -1

        try:
            pk = PowerKey()
            if pk.powerKeyEventRegister(_powerkey_callback) == 0:
                _log.info("PowerKey: short=cycle, long=settings/confirm, 1500ms.")
                oled_status("PowerKey Register ok")
            else:
                _log.warning("PowerKey register failed.")
                oled_status("PowerKey Register fail")
        except Exception as e:
            _log.warning("PowerKey init error: %s" % e)
            oled_status("PowerKey Init err")
        oled_display.clear(oled_i2c)
        try:
            while True:
                try:
                    status_led.tick()
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
                    if _powerkey_fota_requested:
                        _log.info("PowerKey: FOTA selected, enter FOTA.")
                        oled_status("FOTA...")
                        try:
                            if fota_update:
                                fota_update.run_fota_with_progress(
                                    oled_status_cb=oled_status,
                                    log_info_cb=_log.info,
                                )
                            else:
                                _log.warning("fota_update not available")
                                oled_status("FOTA module n/a")
                        except Exception as fota_err:
                            _log.error("FOTA error: %s" % fota_err)
                            oled_status("FOTA err, restart")
                        break
                    if _powerkey_exit_requested:
                        _log.info("PowerKey: Power off selected, exit.")
                        oled_status("Power off...")
                        shutdown_requested = True
                        break
                    if tick % FLASH_CHECK_INTERVAL_TICKS == 0 and is_flash_mode(flash_pin):
                        _log.info("Flash pin asserted, exit.")
                        oled_status("Flash mode exit")
                        break

                    _pk_chain_tick("A_loop_iter_start")

                    # 1) 优先 GNSS；无 GNSS 时尽快 LBS 一次，之后按 lbs_interval 刷新，直到 GNSS 恢复
                    gnss_read_once()
                    _pk_chain_tick("B_after_gnss_read_once")
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
                    # 有有效位置且 fix 非 0 则为 GNSS；否则已由 LBS 分支设为 LBS
                    if lat is not None and lon is not None and gps_data.get("fix") != "0":
                        gps_data["_source"] = "GNSS"
                    _pk_chain_tick("B1_after_lbs_and_source")
                    # 显示：熄屏用 mode3，设置页用菜单单行，否则三款信息页轮播
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
                            _t_bat0 = utime.ticks_ms()
                            bat_pct, _ = battery.get_battery()
                            _pk_chain_tick(
                                "C_battery_get_ms=%d" % utime.ticks_diff(utime.ticks_ms(), _t_bat0)
                            )
                        except Exception:
                            pass
                    try:
                        loc = utime.localtime()
                        system_time_str = "%04d-%02d-%02d %02d:%02d:%02d" % (loc[0], loc[1], loc[2], loc[3], loc[4], loc[5])
                    except Exception:
                        system_time_str = "--:--:--"
                    aprs_ago_sec = (now - last_aprs_ts) if last_aprs_ts else None
                    if speed_kmh <= still_speed_threshold:
                        traccar_ago_sec = (now - last_still_report_ts) if last_still_report_ts else None
                    else:
                        traccar_ago_sec = (now - last_report_ts) if last_report_ts else None
                    _pk_chain_tick("D_after_prep_before_oled")
                    if _is_screen_off():
                        oled_display.update_display(oled_i2c, 3, 0)
                    elif _in_settings:
                        if not prev_in_settings or prev_settings_option != _settings_option:
                            oled_display.clear(oled_i2c)
                            for i in range(3):
                                line = ("> " if i == _settings_option else "  ") + SETTINGS_OPTIONS[i]
                                oled_display.show_boot_message(oled_i2c, line)
                        prev_in_settings = True
                        prev_settings_option = _settings_option
                    else:
                        prev_in_settings = False
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
                    _pk_chain_tick("F_after_oled_returns")
                    if _pk_chain_t0_ms is not None:
                        _pk_chain_t0_ms = None
                    if lat is None or lon is None:
                        utime.sleep(1)
                        continue

                    # APRS：有位置且间隔到时则入队（异步上报在 aprs_report.py）
                    if aprs_report and aprs_cfg.get("aprs_callsign"):
                        aprs_interval = aprs_cfg.get("aprs_interval", 60)
                        if (now - last_aprs_ts) >= aprs_interval:
                            aprs_report.enqueue(gps_data)
                            last_aprs_ts = now

                    # 2) 间隔：速度≤阈值按静止间隔，否则按运动间隔；若配置了距离阈值，低速但移动距离超阈值则强制上报一次
                    if now - last_report_ts < moving_interval:
                        utime.sleep(1)
                        continue
                    last_report_ts = now
                    force_report_by_distance = False
                    if (
                        distance_threshold > 0
                        and last_report_lat is not None
                        and last_report_lon is not None
                    ):
                        try:
                            # Haversine 距离（米）
                            r_earth = 6371000.0
                            lat1 = math.radians(last_report_lat)
                            lon1 = math.radians(last_report_lon)
                            lat2 = math.radians(lat)
                            lon2 = math.radians(lon)
                            dlat = lat2 - lat1
                            dlon = lon2 - lon1
                            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
                            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                            distance_m = r_earth * c
                            if speed_kmh <= still_speed_threshold and distance_m >= distance_threshold:
                                force_report_by_distance = True
                        except Exception:
                            force_report_by_distance = False
                    if (
                        not force_report_by_distance
                        and speed_kmh <= still_speed_threshold
                        and last_still_report_ts != 0
                        and (now - last_still_report_ts) < still_interval
                    ):
                        utime.sleep(1)
                        continue
                    last_still_report_ts = now

                    # 打点：构造 Traccar 载荷并入队（rssi/cell/battery 从全局缓存读，不阻塞）
                    payload = build_traccar_payload(device_id, lat, lon, gps_data)
                    if traccar_report:
                        traccar_report.enqueue(payload)
                    last_report_lat = lat
                    last_report_lon = lon

                    utime.sleep(1)
                except Exception as loop_err:
                    _log.error("main_loop error: %s" % loop_err)
                    oled_status("err:" + str(loop_err)[:17])
                    utime.sleep(2)
        finally:
            status_led.off()
            oled_status("exit.")
            # SSD1327 整帧 SPI 刷新后立刻 clear 会马上黑屏，肉眼像「从未亮过」
            try:
                utime.sleep_ms(500)
            except Exception:
                pass
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
            _log.info("GNSS_Reporter exit.")
            if _powerkey_exit_requested or shutdown_requested:
                Power.powerDown()
    except NeedRestart as e:
        status_led.set_mode(StatusLed.MODE_ERROR)
        _log.error("NeedRestart: %s" % e)
        oled_status("PowerDown")
        shutdown_requested = True
    except Exception as e:
        status_led.set_mode(StatusLed.MODE_ERROR)
        _log.error("Exception: %s" % e)
        oled_status("Exception:" + str(e)[:17])
    finally:
        status_led.off()
        oled_status("exit.")
        try:
            utime.sleep_ms(500)
        except Exception:
            pass
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
        _log.info("GNSS_Reporter exit.")
        if _powerkey_exit_requested or shutdown_requested:
            Power.powerDown()


if __name__ == "__main__":
    main()
