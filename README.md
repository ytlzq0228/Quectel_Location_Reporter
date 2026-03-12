# Quectel 定位上报 Traccar / APRS

在移远 **QuecPython** 平台（如 EC800M）上运行的 GNSS 定位上报项目：从模组内置 GNSS 获取位置，上报到 **Traccar** 与/或 **APRS**。**只要带 GNSS 的移远 QuecPython 模组均可运行**，无需树莓派。

---

## 参考文章

- **前作（树莓派）**：[RPI_APRS 树莓派 GNSS 上报 APRS](https://blog.csdn.net/ytlzq0228/article/details/145265823)
- **本作（4G 模组）**：[移远 EC800M + Traccar 定位上报](https://blog.csdn.net/ytlzq0228/article/details/158775612)（CSDN · ytlzq0228）  
  含模组选型、刷固件、传脚本、上电方式等完整教程与配图。

---

## 项目升级：从树莓派到单板 4G 模组

原先在树莓派上跑的 GNSS 上报（Traccar / APRS）已迁移到 **单板 MCU** 上，用 **移远 QuecPython 模组** 即可完成全部功能，不必再依赖树莓派。

**用 4G 模组做 MCU 的优势：**

| 对比项     | 树莓派       | 4G 模组（如 EC800M）   |
|------------|--------------|------------------------|
| 通信       | 需外接 4G/ WiFi | **模组自带 4G LTE**    |
| 架构       | 板子 + 模组  | **MCU + GNSS + 4G 一体** |
| 开机与上报 | 系统启动较慢 | **秒开机，定位后即上报** |
| 电源       | 外接电源     | **开发板带充放电管理，ADC 可采电池电压** |
| 功耗       | 约 3–5 W     | **约 0.5–1 W，小电池也能长续航** |
| 体积       | 较大         | **尺寸小巧**           |

同时模组支持I2C通信，这不得搞一块OLED屏幕上去，现在该项目支持OLED屏幕显示状态，单击电源按钮切换屏幕显示。

![在这里插入图片描述](https://i-blog.csdnimg.cn/direct/cab1147b3331423faa3a5f513acb7918.png) ![在这里插入图片描述](https://i-blog.csdnimg.cn/direct/d21386e3ab024a46904e0f657f3ccfa2.png) ![在这里插入图片描述](https://i-blog.csdnimg.cn/direct/fee1d246343749e991514dcd5b4d7373.png) 

---

## 项目简介

本项目在 EC800M（或其它带 GNSS 的 QuecPython 模组）上读取内置 GNSS 的 NMEA 数据，解析经纬度、速度、航向、海拔等，按**运动/静止策略**上报到 **Traccar**（OSM 协议），并可同时上报 **APRS**。设备 ID 使用模组 IMEI。支持弱网缓存、刷机引脚控制、看门狗等。Traccar/APRS 采用**异步生产-消费**上报，主循环只打点入队不阻塞；rssi/cell 等低频耗时信息由**独立线程**写缓存，主循环只读；LBS 与 cell 信息共用底层时通过**锁串行化**避免冲突（详见下方架构说明）。

---

## 目录（对应博客教程）

- [一、模组选择](#一模组选择)
- [二、刷 Python 固件](#二刷-python-固件)
- [三、传脚本](#三传脚本)
- [四、修改上电方式（可选）](#四修改上电方式可选)
- [项目结构](#项目结构)
- [功能概览](#功能概览)
- [架构与实现要点](#架构与实现要点)
- [配置说明（config.cfg）](#配置说明configcfg)
- [依赖与参考](#依赖与参考)
- [常见坑 / 避坑指南](#常见坑--避坑指南)

---

## 一、模组选择

本文以 **EC800M** 为例（淘宝约 70+ 元），支持锂电池充放电管理。

- **务必购买带 GNSS 的版本**，例如：**EC800MCNGB 双排针定位带充电功能核心板（QTME0076DP）**。
- 移远支持 **GNSS + LBS 基站定位 + QuecPython** 的模组较多，选型可查：  
  [移远蜂窝模组产品页](https://developer.quectel.com/pro-cat-page/cellular-modules)

---

## 二、刷 Python 固件

### 1. 准备开发工具 QPYcom

- 下载地址：[移远资源下载 - QuecPython](https://developer.quectel.com/resource-download?cid=6)

### 2. 下载 QuecPython 固件

- 同上页面，选择与**模组型号一致**的固件下载（如 EC800M）。

### 3. 刷固件

- 使用 **QPYcom** 连接模组的 **AT 串口**，选择固件文件并下载。
- 下载完成后模组重启，在 QPYcom 中切到 **REPL 串口**，在交互标签中确认 Python 已运行。

---

## 三、传脚本

1. **修改配置文件 `config.cfg`**  
   根据你的 Traccar 服务器、APRS 呼号、上报间隔等修改（格式 `key=value`，`#` 为注释）。主要项：
   - `traccar_host` / `traccar_port`（留空 `traccar_host` 则不上报 Traccar）
   - `aprs_callsign` / `aprs_passcode` / `aprs_host` / `aprs_port` / `aprs_interval`（留空 `aprs_callsign` 则不上报 APRS）
   - `moving_interval`、`still_interval`、`still_speed_threshold`、`cache_file`、`flash_gpio`、`wdt_period` 等（见下方配置说明）。

2. **把以下文件拷入模组**（如通过 QPYcom 文件管理）：
   - 将仓库中的 `config.cfg.example` 复制为 `config.cfg` 并按需修改（勿提交含真实 token 的 `config.cfg`）
   - `config.py`（统一配置读取）
   - `GNSS_Reporter.py`（主程序逻辑）
   - `main.py`（入口，可选：也可直接运行 `GNSS_Reporter.py`）
   - 若使用 APRS：`aprs_report.py`
   - 若使用电池/基站信息：`battery.py`、`cell_info.py`（可选）

3. 在 REPL 或开机自启中运行 **`main.py`** 或 **`GNSS_Reporter.py`** 即可。

---

## 四、修改上电方式（可选）

模组默认上电即开机。若需**用引脚控制开关机**：

- 将图中 **1** 处 PWK 接地电阻去掉；
- 将 **2** 处 RST 的 0 Ω 电阻改接到 **3** 的 PWRKEY 处，实现 **PWRKEY 引脚控制开关机**。

具体引脚与 PCB 位置以开发板手册为准（可参考博客配图）。

---

## 项目结构

```
.
├── config.cfg.example  # 配置示例（复制为 config.cfg 后修改，勿提交真实配置）
├── config.py           # 统一配置读取（Traccar/APRS/LBS）
├── main.py             # 启动入口
├── GNSS_Reporter.py    # 主程序逻辑（GNSS + LBS + Traccar + APRS + 缓存 + 刷机检测 + 额外缓存线程）
├── traccar_report.py   # Traccar 上报模块（Queue + 消费者线程 + 备份线程）
├── aprs_report.py      # APRS 上报模块（Queue + 消费者线程 + 备份线程，可选）
├── battery.py          # 电池/电源信息（可选）
├── cell_info.py        # 基站信息（可选，与 LBS 共用底层时由锁串行化）
├── oled_display.py     # OLED 显示（可选）
├── Fonts/              # 点阵字库（如 font_to_py 生成的 PixelOperator_12/32）
├── tools/
│   ├── font_to_py.py   # PC 端：TTF 转 Python 字库（Freetype，见 docs/OLED_FONTS.md）
│   └── *.ttf / *.ttc   # 字体源文件
├── docs/
│   └── OLED_FONTS.md   # OLED 字体方案说明
├── test_oled.py        # OLED 显示测试（可选）
└── README.md
```

**运行核心**：`config.cfg` + `config.py` + `GNSS_Reporter.py`（或通过 `main.py` 启动），其余为可选模块。

### OLED 使用外部字体

OLED 当前使用 **font_to_py** 生成的 12px/32px 字库（见 `Fonts/`）。若需其它尺寸或字体：

1. **在 PC 上生成字库**（需 Freetype：`pip install freetype-py`）：
   ```bash
   python tools/font_to_py.py tools/PixelOperator.ttf 16 Fonts/font_16.py
   ```
   详见 [docs/OLED_FONTS.md](docs/OLED_FONTS.md) 中的方案一（font-to-py + Writer）与方案二（本项目点阵工具）。

2. **将生成的文件放到设备**：把字库拷到模组 `Fonts/` 或 `/usr/Fonts/`，`oled_display` 按项目约定加载。

更多方案（含 MicroPython 下用 TTF 高质量绘制的常见做法）见 [docs/OLED_FONTS.md](docs/OLED_FONTS.md)。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **GNSS** | 使用 `quecgnss` 读取 NMEA（GGA/RMC），解析经纬度、速度、航向、海拔、卫星数等 |
| **Traccar 上报** | HTTP GET 上报到 Traccar（OSM 协议），设备 ID 为模组 IMEI |
| **APRS 上报** | 可选；配置 `aprs_callsign` 等后，按 `aprs_interval` 上报（最小间隔 30 秒） |
| **运动/静止策略** | 运动时按 `moving_interval`，静止时按 `still_interval`；速度超过 `still_speed_threshold`（km/h）视为运动 |
| **弱网与缓存** | 发送失败时写入 `cache_file`，网络恢复后先发缓存再发新点，带退避重试 |
| **刷机控制引脚** | 检测 `flash_gpio`：未悬空（如接 GND）则程序退出，便于进入刷机模式 |
| **看门狗** | `wdt_period` > 0 时启用，超时未喂狗则重启 |

---

## 架构与实现要点

### 1. 异步生产-消费机制（Traccar / APRS）

Traccar 与 APRS 上报均采用**生产-消费**异步模式，主循环只负责按间隔打点、入队，不直接发网络，避免阻塞定位与打点节奏。

- **生产者**：主程序（`GNSS_Reporter.py`）按运动/静止策略构造点位，只调用 `traccar_report.enqueue(payload)` / `aprs_report.enqueue(gps_data)` 将一条记录放入模块内维护的 **Queue**。
- **消费者**：各模块内独立**消费者线程**，只从 Queue 取任务、发送 HTTP/TCP；**发送成功**则丢弃；**可重试错误**（如 5xx、超时）则按退避时间（如 `attempts * 5` 秒，上限 `max_backoff`）写入 `next_ts`，再 **put 回队列**，到点后再次取出重试。
- **持久化**：另有**备份线程**定期将队列全量同步到文件（先全量取出 → 写文件 → 再 put 回队列）；进程启动时从该文件加载到队列，弱网或重启后不丢点。

主进程只与 Queue 交互，发送与重试逻辑在模块内闭环，对主流程无阻塞。

### 2. LBS 与 cell_info 底层冲突的退避机制

LBS 基站定位（`cellLocator.getLocation`）与 Traccar 附加信息里的**小区信息**（`cell_info.get_cell_info()`）会共用模组底层 cell 相关接口，**并发调用易导致底层冲突或异常**。

项目通过**一把全局锁**串行化两处访问：

- **LBS 侧**（主线程）：调用 LBS 前**阻塞持锁**（`lock.acquire()`），再执行 `cellLocator.getLocation`，结束后释放锁。保证 LBS 执行期间不会有 cell 读取干扰。
- **rssi/cell 缓存刷新侧**（独立线程）：该线程定期拉取 rssi、cell 写入全局缓存。刷新时**非阻塞尝试拿锁**（`lock.acquire(0)`）；拿不到说明 LBS 正在用，则**跳过本轮刷新**，等下一周期再试，从而不阻塞 LBS，也不与 LBS 同时碰底层。

这样 LBS 与 cell 刷新在底层互斥，避免冲突；主循环打点仍只读缓存，不因拿锁而卡顿。

### 3. 主进程中耗时长的低频更新单独线程

rssi（信号强度）、cell（小区信息）、电池等属于**低频、高延迟**的查询（一次可能数百毫秒），若在主循环里同步拉取，会拖慢高密度打点（如运动时 1 秒一点）。

项目将这类**耗时且低频**的更新放到**独立线程**中：

- 线程 `_traccar_extra_cache_loop` 每 **10 秒**拉取一次 rssi、cell（及可选 battery），写入全局字典 `_traccar_extra_cache`。
- 主循环构造 Traccar 载荷时，只从 `_traccar_extra_cache` **读**当前缓存值，不在此做任何阻塞调用。
- 该线程与 LBS 共用上述锁，刷新时非阻塞拿锁，拿不到就跳过本轮（见上一节）。

这样主进程只做 GNSS 解析与打点逻辑，高频率；低频、耗时的附加信息由后台线程更新，互不阻塞。

---

## 配置说明（config.cfg）

- **Traccar**：`traccar_host`、`traccar_port`（留空 `traccar_host` 则不发送 Traccar）
- **上报策略**：`moving_interval`、`still_interval`、`still_speed_threshold`
- **APRS**：`aprs_callsign`、`aprs_passcode`、`aprs_host`、`aprs_port`、`aprs_interval`（callsign 留空则不发送 APRS）
- **缓存**：`cache_file` 持久化路径
- **刷机**：`flash_gpio` 引脚号（见 EC800M 硬件手册）
- **网络/HTTP**：`network_timeout`、`http_timeout`、`max_backoff`
- **看门狗**：`wdt_period`（秒），0 表示不启用

格式为 `key=value`，`#` 开头为注释。完整示例见仓库内 `config.cfg`。

---

## 依赖与参考

- **运行环境**：QuecPython（EC800M 或其它带 GNSS 的移远模组），模块：`quecgnss`、`machine.Pin`、`modem`、`usocket`、`dataCall`、`checkNet`、`ujson`、`utime`、`uos` 等。
- **API 参考**：[QuecPython API 参考手册](https://developer.quectel.com/doc/quecpython/API_reference/zh/)。

---

## 说明与注意事项

- 早期在树莓派/PC 上的示例与测试脚本可参考前作博客；当前功能以根目录 `GNSS_Reporter.py`（及 `main.py`）与 `config.cfg` 为准。
- EC800M 内置 GNSS 存在已知问题（如长时间运行偶发异常），可参考移远社区与上述 CSDN 博客中的排坑说明。


**仓库地址**：[https://github.com/ytlzq0228/QuectelEC800_Traccar_Report](https://github.com/ytlzq0228/QuectelEC800_Traccar_Report)
