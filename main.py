# main.py - 主入口：同时进行 Traccar 与 APRS 上报
#
# 主循环与逻辑在 GNSS_Reporter.py；
# APRS 能力在 aprs_report.py，Traccar 能力在 traccar_report.py。

import sys
if "/usr" not in sys.path:
    sys.path.insert(0, "/usr")

import GNSS_Reporter

if __name__ == "__main__":
    GNSS_Reporter.main()
