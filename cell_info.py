# cell_info.py - 获取蜂窝网络小区信息并以 JSON 返回
#
# 参考：https://developer.quectel.com/doc/quecpython/API_reference/zh/iotlib/net.html
# 返回字段：MCC, MNC, LAC, CID, RAT, RSSI(RSRP)
#

import ujson
import net
import modem


def get_cell_info_json():
    """
    获取当前服务小区信息，以 JSON 字符串返回。
    包含：MCC, MNC, LAC, CID, MAC(SN+设备SN), RAT, RSSI/RSRP, RSRQ
    """
    out = {
        "MCC": None,
        "MNC": None,
        "LAC": None,
        "CID": None,
        "MAC": None,   # "SN" + modem.getDevSN()
        "RAT": None,
        "RSSI": None,
        "RSRP": None,
        "RSRQ": None,
    }

    # MAC：使用 "SN" + 设备序列号
    try:
        sn = modem.getDevSN()
        out["MAC"] = "SN:" + (str(sn) if sn is not None else "")
    except Exception:
        pass

    # MCC / MNC：用 operatorName 直接得到 (long_eons, short_eons, mcc, mnc)，已是正确格式
    op = net.operatorName()
    if op is not None and op != -1 and len(op) >= 4:
        out["MCC"] = op[2] if op[2] else None   # 字符串如 '460'
        out["MNC"] = op[3] if op[3] else None   # 字符串如 '01'

    # 服务小区 LAC
    lac = net.getServingLac()
    if lac is not None and lac != -1:
        out["LAC"] = lac

    # 服务小区 CID
    cid = net.getServingCi()
    if cid is not None and cid != -1:
        out["CID"] = cid

    # 网络注册状态中的 RAT (data)
    state = net.getState()
    if state is not None and state != -1 and len(state) >= 2:
        data_part = state[1]
        if len(data_part) >= 4:
            out["RAT"] = data_part[3]  # data_rat

    # 详细信号：([rssi, bitErrorRate, rscp, ecno], [rssi, rsrp, rsrq, cqi, sinr])
    sig = net.getSignal()
    if sig is not None and sig != -1 and len(sig) >= 2:
        lte = sig[1]
        if len(lte) >= 1 and lte[0] not in (99, 255):
            out["RSSI"] = lte[0]
        if len(lte) >= 2 and lte[1] not in (99, 255):
            out["RSRP"] = lte[1]
        if len(lte) >= 3 and lte[2] not in (255,):
            out["RSRQ"] = lte[2]
        if out["RSSI"] is None and out["RSRP"] is not None:
            out["RSSI"] = out["RSRP"]

    return ujson.dumps(out)


def main():
    s = get_cell_info_json()
    print(s)
    return s


if __name__ == "__main__":
    main()
