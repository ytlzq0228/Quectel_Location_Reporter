# cell_info.py - 获取蜂窝网络小区信息
#
# 参考：https://developer.quectel.com/doc/quecpython/API_reference/zh/iotlib/net.html
# 返回格式：mcc,mnc,lac,cellId,signalStrength（逗号分隔字符串）
#

import net

def get_cell_info():
    """
    获取当前服务小区信息，返回格式：mcc,mnc,lac,cellId,signalStrength
    逗号分隔，缺省为空字符串。
    """
    mcc = ""
    mnc = ""
    lac = ""
    cell_id = ""
    signal_strength = ""

    # MCC / MNC
    op = net.operatorName()
    if op is not None and op != -1 and len(op) >= 4:
        if op[2] is not None:
            mcc = str(op[2])
        if op[3] is not None:
            mnc = str(op[3])

    # LAC
    lac_val = net.getServingLac()
    if lac_val is not None and lac_val != -1:
        lac = str(lac_val)

    # CellId (CID)
    cid_val = net.getServingCi()
    if cid_val is not None and cid_val != -1:
        cell_id = str(cid_val)

    # SignalStrength：优先 RSSI，无则用 RSRP
    sig = net.getSignal()
    if sig is not None and sig != -1 and len(sig) >= 2:
        lte = sig[1]
        rssi = None
        if len(lte) >= 1 and lte[0] not in (99, 255):
            rssi = lte[0]
        if len(lte) >= 2 and lte[1] not in (99, 255) and rssi is None:
            rssi = lte[1]  # RSRP
        if rssi is not None:
            signal_strength = str(rssi)

    return ",".join((mcc, mnc, lac, cell_id, signal_strength))


def main():
    s = get_cell_info_json()
    print(s)
    return s


if __name__ == "__main__":
    main()
