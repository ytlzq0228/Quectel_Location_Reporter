# battery.py - EC800M 电池电压与 SOC，供 GNSS_Traccar 等模块 import
# 开发板分压比 ADC:VBAT = 1:4，VBAT = ADC读数(mV) * 4

import utime

try:
    from misc import ADC
    VBAT_ADC_CH = ADC.ADC0
except Exception:
    ADC = None
    VBAT_ADC_CH = None

VBAT_RATIO = 4

VBAT_SOC_TABLE = (
    (4.20, 100),
    (4.18, 99),
    (4.15, 95),
    (4.12, 92),
    (4.10, 90),
    (4.08, 87),
    (4.05, 82),
    (4.02, 76),
    (4.00, 72),
    (3.98, 68),
    (3.95, 60),
    (3.92, 52),
    (3.90, 45),
    (3.88, 38),
    (3.85, 30),
    (3.82, 22),
    (3.80, 18),
    (3.78, 14),
    (3.75, 10),
    (3.72, 7),
    (3.70, 5),
    (3.65, 3),
    (3.60, 2),
    (3.50, 1),
    (3.40, 0),
)


def voltage_to_soc(vbat_v):
    """根据电压(V)估算剩余电量 0~100%，区间内线性插值。"""
    if vbat_v >= VBAT_SOC_TABLE[0][0]:
        return 100.0
    if vbat_v <= VBAT_SOC_TABLE[-1][0]:
        return 0.0
    for i in range(len(VBAT_SOC_TABLE) - 1):
        v0, p0 = VBAT_SOC_TABLE[i]
        v1, p1 = VBAT_SOC_TABLE[i + 1]
        if v1 <= vbat_v <= v0:
            if v0 == v1:
                return float(p0)
            r = (vbat_v - v1) / (v0 - v1)
            return p1 + r * (p0 - p1)
    return 0.0


def _read_vbat_mv(adc, channel, samples=5):
    buf = []
    for _ in range(samples):
        try:
            mv = adc.read(channel)
            buf.append(mv)
        except Exception:
            return None
        utime.sleep_ms(50)
    if len(buf) < 3:
        return sum(buf) // len(buf) if buf else None
    buf.sort()
    return sum(buf[1:-1]) // (len(buf) - 2)


def get_battery():
    """
    读取电池电压与剩余电量。
    返回 (batteryLevel, batteryVoltage)：
      - batteryLevel: 0~100 的浮点数（百分比，不带%）
      - batteryVoltage: 电压(V) 浮点数
    若 ADC 不可用或读取失败，返回 (None, None)。
    """
    if ADC is None or VBAT_ADC_CH is None:
        return (None, None)
    try:
        adc = ADC()
        adc.open()
        try:
            adc_mv = _read_vbat_mv(adc, VBAT_ADC_CH)
            if adc_mv is None:
                return (None, None)
            vbat_mv = adc_mv * VBAT_RATIO
            vbat_v = (vbat_mv+20) / 1000.0
            soc = voltage_to_soc(vbat_v)
            return (round(soc, 2), round(vbat_v, 2))
        finally:
            try:
                adc.close()
            except Exception:
                pass
    except Exception:
        return (None, None)

if __name__ == "__main__":
    print(get_battery())