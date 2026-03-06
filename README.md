# QuectelEC800_Traccar_Report

在移远 EC800M（QuecPython）上运行的 GNSS 定位上报项目：从内置 GNSS 获取位置，上报到 Traccar 服务器。

## 项目结构（根目录）

```
.
├── config.cfg      # 配置文件（Traccar 地址、端口、上报间隔、缓存路径、刷机 GPIO 等）
├── GNSS_Traccar.py # 主程序入口
└── README.md
```

## 功能

- **GNSS**：使用 `quecgnss` 读取 NMEA（GGA/RMC），解析经纬度、速度、航向、海拔、卫星数等。
- **Traccar 上报**：通过 HTTP GET 将位置上报到 Traccar（OSM/开放协议），设备 ID 自动使用模组 IMEI（`modem.getDevImei()`）。
- **配置**：从 `config.cfg` 读取（key=value，支持 `#` 注释），包括：
  - `traccar_host` / `traccar_port`
  - `moving_interval`、`still_interval`、`still_speed_threshold`（运动/静止策略）
  - `cache_file`（持久化缓存路径）
  - `flash_gpio`（刷机控制引脚）
  - 网络与 HTTP 超时、退避等。
- **运动/静止策略**：运动时按 `moving_interval` 上报，静止时按 `still_interval` 上报；速度超过 `still_speed_threshold`（km/h）视为运动。
- **弱网与持久化缓存**：发送失败或可重试错误时，将点位写入 `cache_file`（每行一条 JSON）；网络恢复后优先发送缓存再发新点，带退避重试。
- **刷机控制引脚**：启动时和运行中周期性检测 `flash_gpio`；若该引脚未悬空（例如接 GND），程序退出，便于进入刷机模式。引脚配置为输入+上拉，悬空为高、拉低为退出。

## 使用

1. 将 `config.cfg`、`GNSS_Traccar.py` 放到设备可执行目录（如与主脚本同目录），并修改 `config.cfg` 中的 Traccar 地址、端口及 GPIO 等。
2. 在 EC800M 上使用 QuecPython 运行主程序：`GNSS_Traccar.py`。
3. 若需从其他路径读配置，可修改脚本中的 `CONFIG_FILE` 或改为从参数/环境读取。

## 依赖与参考

- QuecPython（EC800M），模块：`quecgnss`、`machine.Pin`、`modem`、`usocket`、`dataCall`、`checkNet`、`ujson`、`utime`、`uos`。
- API 参考：[QuecPython API 参考手册](https://developer.quectel.com/doc/quecpython/API_reference/zh/)。

## 说明

- `example/` 目录下为早期在树莓派/PC 上的示例与测试脚本，新功能以根目录的 `GNSS_Traccar.py` 与 `config.cfg` 为准；若不再需要可删除 `example/`。
