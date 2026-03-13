# test_quecgnss.py - 单独测试 QuecPython GNSS：初始化、读 NMEA、解析 GGA/RMC 并输出日志

import utime
import quecgnss
import log

log.basicConfig(level=log.INFO)
_log = log.getLogger("test_quecgnss")

# ------------------------- GNSS 解析（与 GNSS_Reporter 主流程一致）-------------------------
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
    "accuracy": None,
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
            _log.debug("GGA: %s" % line)
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
            _log.debug("RMC: %s" % line)
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
        elif line.startswith("$") and len(line) >= 6 and line[3:6] == "GLL":
            _log.debug("GLL: %s" % line)
        elif line.startswith("$") and len(line) >= 6 and line[3:6] == "VTG":
            _log.debug("VTG: %s" % line)
        elif line.startswith("$") and len(line) >= 6 and line[3:6] == "GSA":
            _log.debug("GSA: %s" % line)
        elif line.startswith("$") and len(line) >= 6 and line[3:6] == "GSV":
            _log.debug("GSV: %s" % line)



def main():
    # 与主流程一致的 GNSS 初始化
    try:
        quecgnss.configSet(0,5)#设置定位星系为GPS+Beidou
        quecgnss.configSet(1,63)
        quecgnss.configSet(2,1)#打开AGPS
        quecgnss.configSet(3,1)#使能APFLASH
        quecgnss.configSet(4,1)#打开备电
    except Exception as e:
        _log.warning("quecgnss configSet: %s" % e)
    ret = quecgnss.init()
    if ret != 0:
        _log.error("GNSS init failed, ret: %s" % ret)
        return -1
    _log.info("GNSS init ok, reading... (Ctrl+C to stop)")

    try:
        while True:
            gnss_read_once()
            lat = gps_data.get("lat")
            lon = gps_data.get("lon")
            fix = gps_data.get("fix", "0")
            sats = gps_data.get("sats", 0)
            speed_kmh = gps_data.get("speed", 0)
            acc = gps_data.get("accuracy")
            track = gps_data.get("track")
            alt = gps_data.get("alt")
            if lat is not None and lon is not None:
                _log.info(
                    "lat=%.6f lon=%.6f fix=%s sats=%d speed=%.2f km/h acc=%s m track=%s alt=%s"
                    % (lat, lon, fix, sats, speed_kmh, acc, track, alt)
                )
            else:
                _log.debug("no fix yet (fix=%s sats=%d)" % (fix, sats))
            utime.sleep(1)
    except KeyboardInterrupt:
        _log.info("Ctrl+C, exit.")
    finally:
        try:
            quecgnss.gnssEnable(0)
        except Exception:
            pass
        _log.info("GNSS test exit.")
    return 0


if __name__ == "__main__":
    main()
