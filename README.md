# N9020A + SDS3104X HD 联合采集工具

这是一个 PySide6 桌面工具，用于联合采集 Keysight N9020A 与 SIGLENT SDS3104X HD：

- 采集阶段：N9020A 只保存 CSV；SDS3104X HD 只保存 NPZ，并保存高级测量 `DELAY` 元数据。
- 后处理阶段：用户手动生成 Scope CSV、N9020A/Scope PNG、DELAY 汇总和离线 HTML 报告。
- 自动恢复：N9020A 或 Scope 发生 VISA/I/O/timeout/connection reset 后，按配置等待并重连，再完整重采当前 index。

采集 Worker 不导入 Matplotlib 或 Plotly，不生成图片、HTML 或 Scope CSV。数据导出与采集线程完全解耦。

## 安装与运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

依赖包括 PySide6、PyVISA、NumPy、Matplotlib 和 Plotly。Matplotlib/Plotly 仅由离线后处理按需导入。

## 配置

`config.json` 保存设备与时序参数；`collector_state.json` 只保存 GUI 状态、地址、目录和当前编号。打包程序按以下顺序查找配置：

1. exe 同目录的 `config.json`
2. 当前工作目录的 `config.json`
3. 源码运行时的项目目录 `config.json`

当前默认配置：

```json
{
  "scope": {
    "ip": "192.168.1.50",
    "channel": "C1",
    "single_timeout_sec": 30.0,
    "trigger_poll_interval_ms": 50,
    "delay_settle_delay_ms": 200,
    "delay_retry_delay_ms": 200,
    "delay_max_attempts": 3,
    "visa_timeout_ms": 60000,
    "chunk_size_mb": 20,
    "reconnect_enabled": true,
    "reconnect_delay_sec": 2.0,
    "reconnect_max_attempts": 5
  },
  "n9020a": {
    "visa_timeout_ms": 5000,
    "reconnect_enabled": true,
    "reconnect_delay_sec": 15.0,
    "reconnect_max_attempts": 5
  },
  "acquisition": {
    "max_sample_recovery_attempts": 5
  },
  "delay_measurement_test": {
    "delays_ms": [0, 50, 100, 150, 200, 300, 500],
    "samples_per_delay": 50,
    "inter_sample_delay_ms": 200
  }
}
```

主要参数：

- `scope.delay_settle_delay_ms`：Trigger Stop 后到查询 DELAY 的等待时间。
- `scope.delay_retry_delay_ms`：DELAY 无效后、下一次新 Single 前的等待时间。
- `scope.delay_max_attempts`：单个样本因 `****`/`---` 等无效测量而执行新 Single 的上限，默认 3。
- `scope.reconnect_delay_sec`：Scope 通信失败后的重连等待，默认 2 秒。
- `n9020a.reconnect_delay_sec`：N9020A 通信失败后的重连等待，默认 15 秒，用于覆盖频谱仪可能正在进行的自动校准时间。
- `*.reconnect_max_attempts`：单次恢复时的重连上限，默认 5。
- `acquisition.max_sample_recovery_attempts`：一个样本因通信故障而整组重采的上限，默认 5；它与 DELAY 无效重试是两套独立计数。

旧配置中的 `pwid_settle_delay_ms`、`pwid_retry_delay_ms`、`pwid_max_attempts` 和 `pwid_delay_test` 仍可读取，并映射到新 DELAY 配置；新旧字段同时存在时新字段优先。新生成的配置和本文档只使用 DELAY 命名。

主界面“采集间隔”只控制 `000001` 与 `000002` 两个正式样本之间的节奏，不参与 DELAY settle/retry 或仪表重连等待。

## DELAY 高级测量

Scope 连接成功、正式任务开始前，以及 Scope 重连成功后，程序都会主动发送：

```text
MEAS:ADV:P1:TYPE DELAY
```

每次新 Single 停止后查询：

```text
MEAS:ADV:P1:VAL?
```

有效有限浮点数按 second 解析，例如 `1.3447E-07` 保存为 `1.3447e-07`。空字符串、`---`、包含 `*` 的响应、无法转换或非有限值统一为 `NaN`。

DELAY 无效时会等待 `delay_retry_delay_ms` 并执行一个全新的 Single，而不是在同一帧连续查询。默认最多 3 次；最后仍无效时保存最后一次 Single 的波形以及 `delay_s=NaN`，整组 CSV/NPZ 仍可成功提交。通信 timeout 与无效测量不同，会进入仪表重连和整组重采流程。

GUI 使用统一时间格式显示，例如 `1.3447e-7 → 134.47 ns`、`2.3e-6 → 2.30 μs`、`NaN → N/A`。NPZ 的权威单位始终是 second。

## 联合采集与自动恢复

每组正常顺序：

1. Scope Single，并等待 Trigger Status 为 Stop。
2. 等待 settle 时间，读取 `MEAS:ADV:P1:VAL?`；必要时用新 Single 重试。
3. 读取 N9020A CSV。
4. 读取最后保留的 Scope 波形。
5. 写入 `*.csv.tmp`、`*.npz.tmp`，再原子提交同编号 CSV/NPZ。
6. 更新当前目录的 `delay_summary.csv`，成功后 GUI 才递增 `current_index`。

Zero Span 模式按频率建目录：

```text
data/
  600.000MHz/
    000001.csv
    000001.npz
    delay_summary.csv
  605.000MHz/
    000001.csv
    000001.npz
    delay_summary.csv
```

驱动只负责识别并抛出明确的 `N9020ACommunicationError` 或 `ScopeCommunicationError`；Worker 决定等待、重连和整组重采。业务数据 `ValueError` 不会被笼统标记为掉线。

通信恢复规则：

- N9020A：断开损坏的 VISA session，先可中断地等待默认 15 秒，再进行最多 5 次 `connect + *IDN?`；每次失败后再次等待。日志会提示可能是自动校准或临时断线。
- SDS3104X HD：断开 session，等待默认 2 秒，重连并验证 `*IDN?`，随后重新执行 `MEAS:ADV:P1:TYPE DELAY`。
- 恢复成功后清理当前 index 的 `.tmp`/孤立文件，从 Scope Single 开始完整重采同一个 index；不会续用半组数据，也不会提前递增编号。
- 重连等待每最多约 100 ms 检查一次停止请求，不调用 `QThread.terminate()`。
- 单样本超过 `max_sample_recovery_attempts` 或重连次数上限后，连续采集停止并向用户报告错误，避免无限循环。

日志统一包含 `[N9020A]`、`[SDS3104XHD]`、`[ACQUISITION]` 和六位采集编号，可区分校准等待、重连、成功及当前样本重启。

本轮不通过“连续 CSV 完全相同”判断 N9020A 校准，因为稳定信号本来就可能产生相同数据。

## 新 NPZ 字段

新采集 NPZ 使用 `numpy.savez_compressed()`，主要字段：

- `index`：`int32`
- `time_s`：`float64`，second
- `voltage_v`：`float32`，volt
- `adc`：`int16`
- `delay_s`：`float64`，second；不可用时为 `NaN`
- `delay_raw`：Unicode string，例如 `1.3447E-07` 或 `****`
- `delay_valid`：`bool`
- `delay_attempts`：`int32`
- `advanced_measurement_type`：字符串 `DELAY`
- `device`、`ip`、`channel`、`point_count`
- `adc_bit`、`vdiv`、`offset`、`probe`、`code_per_div`
- `interval`、`sample_rate`、`tdiv`、`delay`

新文件不再写入 `positive_pulse_width_*`。后处理仍可读取旧 NPZ：存在 `positive_pulse_width_s` 时明确标记为 `PWID (Legacy)`，不会把旧正脉宽静默当成 DELAY。

## DELAY 汇总与旧 PWID 兼容

新采集只写 `delay_summary.csv`：

```text
index,delay_s,delay_ns,valid,attempts,raw_value,npz_file,csv_file
```

已有 `pulse_width_summary.csv` 不会被删除或改写为 DELAY。分析选择规则：

- 检测到 `delay_summary.csv`：按 DELAY 分析，输出 `delay_all.csv`、`delay_by_frequency.csv` 和 `delay_report.html`。
- 仅有 `pulse_width_summary.csv`：作为旧版正脉宽分析，HTML 明确显示 `Measurement Type: PWID (Legacy)`。
- DELAY 与 legacy PWID 不会合并进同一组统计或同一份报告。

统计保留 mean、median、population std、min、max、P05、P95、有效率和缺失文件数，数值列统一使用 ns 便于阅读，`NaN` 不参与数值统计但仍计入总数和无效数。

## 数据导出与图片

主界面“数据导出与分析”与采集完全独立：选择数据根目录、输出目录、处理范围和输出类型后，手动点击“开始处理”。批量任务运行在独立 QThread 中，单文件失败会继续；默认不覆盖已有文件，停止会在当前文件完成后安全退出。

支持：

- Scope NPZ → CSV：`000001_scope.csv`
- N9020A CSV → PNG：`000001_csv.png`
- Scope NPZ → PNG：`000001_npz.png`
- 频点级/全局 DELAY 汇总和完全离线 Plotly HTML 报告

新 NPZ 图片显示 `Measurement Type: DELAY` 和 `Delay: 134.47 ns`；旧文件显示 `Measurement Type: PWID (Legacy)`，不存在或无效值显示 `N/A`。大波形用最多 100,000 点的 min/max envelope 预览，保留窄峰能力优于简单步进；原始 NPZ 不会被修改。

N9020A CSV 会跳过 metadata/header，并优先选择 Frequency/Time 与 Amplitude/Power 数值列。普通频谱显示 Frequency，Zero Span 显示 Time。输出统一为 12×6 inch、150 DPI PNG。

## DELAY 测量延时测试工具

`tools/delay_measurement_tester.py` 比较不同“Trigger Stop → DELAY Query”等待时间的成功率：

```powershell
python tools/delay_measurement_tester.py --help
python tools/delay_measurement_tester.py
python tools/delay_measurement_tester.py --samples 20 --delays 50,100,150,200
```

工具主动配置 `MEAS:ADV:P1:TYPE DELAY`，每个测试样本只执行一次 Single 和一次 `MEAS:ADV:P1:VAL?`；不读取波形、不保存 NPZ、不生成图片、不访问 N9020A。结果写入：

- `tools/output/delay_measurement_test_results.csv`
- `tools/output/delay_measurement_test_details.csv`

它推荐成功率至少 99% 的最短 settle delay；若均未达到，则显示成功率最高的最短候选，但不会自动修改 `config.json`。旧 `tools/pwid_delay_tester.py` 暂时保留为兼容入口，新 Release 只发布 `delay-measurement-tester`。

## Windows Release

每个版本 Tag 的 GitHub Release 同时包含两个独立包：

- `interference-source-identidication-<TAG>-windows.zip`：主程序 exe + `config.json`
- `delay-measurement-tester-<TAG>-windows.zip`：测试工具 exe + `config.json` + `README-DELAY-TEST.txt`

解压后修改 exe 旁边的 `config.json` 即可生效。

## 测试

```powershell
python -m compileall .
python -m pytest
python tools/delay_measurement_tester.py --help
```

自动测试覆盖 DELAY 命令、解析/NaN/三次新 Single、NPZ/summary/legacy 兼容、图片与 HTML 文案、通信异常分类、N9020A 15 秒等待、双仪表重连、Scope 重配 DELAY、整组同 index 重采、临时文件清理、恢复次数上限和等待中快速停止。真实仪表的网络、固件 SCPI 兼容性、自动校准行为、Trigger 时序、最佳 settle delay 及长时间连续采集仍需连接真实 N9020A/SDS3104X HD 验证。
