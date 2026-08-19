# N9020A + SDS3104X HD 联合采集工具

这是一个 PySide6 桌面采集与离线分析工具。每个新样本固定执行一次 N9020A 采集和两次相互独立的 SDS3104X HD Single：

```text
000001.csv          N9020A 数据
000001_delay.npz    500 ns/div 的 DELAY 与对应波形
000001_cycles.npz   100 μs/div 的 CYCLES 与对应波形
```

采集 Worker 只保存原始 CSV/NPZ，不导入 Matplotlib/Plotly，也不自动生成图片、Scope CSV 或 HTML；“数据导出与分析”在独立 QThread 中手动运行，与实时采集完全解耦。

## 安装与运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

依赖包括 PySide6、PyVISA、NumPy、Matplotlib 和 Plotly。打包后的程序优先读取 exe 同目录的 `config.json`，其次是当前工作目录，源码运行时最后检查项目目录；缺失时使用并写出默认配置。

## 配置

关键默认值如下：

```json
{
  "scope": {
    "ip": "192.168.1.50",
    "channel": "C1",
    "single_timeout_sec": 30.0,
    "trigger_poll_interval_ms": 50,
    "delay_time_scale_sec": 5e-7,
    "cycles_time_scale_sec": 0.0001,
    "measurement_settle_delay_ms": 200,
    "measurement_retry_delay_ms": 200,
    "measurement_max_attempts": 3,
    "visa_timeout_ms": 60000,
    "chunk_size_mb": 20,
    "reconnect_enabled": true,
    "reconnect_delay_sec": 2.0,
    "reconnect_max_attempts": 5
  },
  "n9020a": {
    "visa_timeout_ms": 5000,
    "reconnect_enabled": true,
    "calibration_wait_sec": 15.0,
    "disconnect_reconnect_delay_sec": 2.0,
    "reconnect_max_attempts": 10
  },
  "acquisition": {
    "max_sample_recovery_attempts": 5
  }
}
```

`measurement_*` 同时控制 DELAY 和 CYCLES 的 settle、无效值重试和最大 Single 次数。旧 `delay_settle_delay_ms`、`delay_retry_delay_ms`、`delay_max_attempts` 以及更早的 `pwid_*` 字段仍能映射；新旧字段并存时 `measurement_*` 优先。主界面“采集间隔”只控制相邻正式样本，不参与测量重试或重连等待。

## 每组采集顺序与原子提交

严格顺序为：

1. N9020A 获取一次 CSV。
2. Scope 发送 `:TIM:SCAL 5.00E-7` 和 `MEAS:ADV:P1:TYPE DELAY`。
3. Scope 执行 Single、等待 STOP、settle 后查询 `MEAS:ADV:P1:VAL?`，随即读取该帧波形。
4. Scope 发送 `:TIM:SCAL 1.00E-4` 和 `MEAS:ADV:P1:TYPE CYCLES`。
5. Scope 再执行一次独立 Single、查询 P1，随即读取第二帧波形。
6. 写入 `000001.csv.tmp`、`000001_delay.npz.tmp`、`000001_cycles.npz.tmp`。
7. 三个临时文件都成功后才提交正式文件并更新 `measurement_summary.csv`；GUI 随后递增编号。

DELAY 的单位是 second，GUI/图片按量级显示 ns/μs 等；CYCLES 的单位是 `count`，保持浮点值，绝不经过时间格式化或乘以 `1e9`。空字符串、`---`、包含 `*`、非数字和非有限响应都转换成 `NaN` 并记 warning。每种测量独立用新 Single 重试，默认最多 3 次；全部无效时仍保存最后一次 Single 的波形，整个样本可以正常完成。

启用 Scope 时，一个新样本只有同时存在 CSV、`_delay.npz` 和 `_cycles.npz` 才算完整。中断、通信故障或保存失败会清理三个 tmp 以及不完整正式组，再从同一 index 的 N9020A 步骤完整重采。旧 `000001.npz` 不会被误判为新的三文件完整组。

Zero Span 目录示例：

```text
data/
  600.000MHz/
    000001.csv
    000001_delay.npz
    000001_cycles.npz
    measurement_summary.csv
  605.000MHz/
    000001.csv
    000001_delay.npz
    000001_cycles.npz
    measurement_summary.csv
```

## NPZ 与测量汇总

两个新 NPZ 都保存波形字段 `index`、`time_s`、`voltage_v`、`adc`，设备/通道信息以及 `point_count`、`sample_rate`、`tdiv` 等现有 metadata。通用测量字段为：

- `measurement_type`：`DELAY` 或 `CYCLES`
- `measurement_value`：`float64`；DELAY 为 second，CYCLES 为 count
- `measurement_unit`：`s` 或 `count`
- `measurement_raw`：仪表原始文本
- `measurement_valid`：`bool`
- `measurement_attempts`：`int32`

DELAY NPZ 额外保留 v0.0.10 的 `delay_s`、`delay_raw`、`delay_valid`、`delay_attempts`，便于旧读者继续使用；CYCLES NPZ 不写任何伪时间字段。

`measurement_summary.csv` 以两个 NPZ 为权威来源，可重新扫描配对文件生成，字段为：

```text
index,delay_s,delay_ns,delay_valid,delay_attempts,delay_raw,cycles_count,cycles_valid,cycles_attempts,cycles_raw,delay_npz_file,cycles_npz_file,csv_file
```

旧 `000001.npz`、`delay_summary.csv` 和更早的 `pulse_width_summary.csv` 仍可读取、转 CSV/PNG，并生成 legacy DELAY/PWID 报告；不会自动混入新的双参数统计。

## 数据导出、图片与 HTML

主界面选择数据根目录后可递归处理所有频率子目录；默认不覆盖已有输出，单文件失败继续，停止会在当前文件完成后安全退出。

- N9020A CSV → PNG：`000001_csv.png`
- DELAY NPZ → PNG：`000001_delay.png`
- CYCLES NPZ → PNG：`000001_cycles.png`
- DELAY NPZ → Scope CSV：`000001_delay_scope.csv`
- CYCLES NPZ → Scope CSV：`000001_cycles_scope.csv`

DELAY 图片显示 `Measurement: DELAY` 和例如 `Delay: 134.47 ns`；CYCLES 图片显示 `Measurement: CYCLES` 和例如 `Cycles: 23`。大波形使用最多 100,000 点的 min/max envelope，仅影响预览，不修改原始 NPZ。

新数据的离线 `measurement_report.html` 明确分为 `DELAY Analysis` 和 `CYCLES Analysis` 两个区域。两者分别计算 Total、Valid、Invalid、Valid Rate、Mean、Median、Std、Min、Max、P05、P95，并分别绘制 histogram、sample index、frequency box plot、frequency mean/median trend；DELAY 用 ns，CYCLES 用 count，单位不混用。

## 通信恢复与 Local 控制

- N9020A 的 PyVISA timeout/临时忙显示 `Possible calibration`，使用 `calibration_wait_sec`（默认 15 秒）。
- connection reset、network unreachable、no route、broken pipe 等明确网络错误显示 `Disconnected`，使用 `disconnect_reconnect_delay_sec`（默认 2 秒）。
- 无法归类的错误显示 `Communication unavailable`，不会误称校准。
- Scope 重连只恢复新 VISA session、验证 `*IDN?` 并设置波形通道；DELAY/CYCLES 的时基和 P1 类型由各自采集阶段重新设置。
- 自动恢复成功只写日志/状态并重采当前样本，不发最终 GUI error；只有重连或恢复次数最终耗尽才弹框。
- 用户主动断开 N9020A 或正常退出时，在关闭 VISA 前 best-effort 发送 `SYST:LOC`；自动重连不会发送。即使 `SYST:LOC` 失败也始终尝试关闭 session。

## DELAY 延时测试工具与 Windows Release

`tools/delay_measurement_tester.py` 用于比较 Trigger Stop 后不同等待时间的 DELAY 有效率，不读取完整波形、不保存 NPZ、不访问 N9020A：

```powershell
python tools/delay_measurement_tester.py --help
python tools/delay_measurement_tester.py
```

每个 Tag 的 GitHub Release 包含主程序包和独立测试工具包；两个包都带同版本 `config.json`，解压后修改 exe 旁边的配置即可生效。

## 验证

```powershell
python -m compileall .
python -m pytest
python tools/delay_measurement_tester.py --help
```

自动测试覆盖双时基/双测量命令、两次独立 Single 与对应波形、三文件原子提交、汇总重建、双 PNG/CSV/HTML、旧格式兼容、错误分类、不同恢复等待、Scope 完整重采、无多余 error signal 和 `SYST:LOC`。真实 N9020A/SDS3104X HD 的固件 SCPI 兼容性、网线拔插、自动校准、触发时序以及长时间连续采集仍需实机验证。
