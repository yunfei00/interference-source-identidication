# N9020A + SDS3104X HD 联合采集工具

这是一个基于 PySide6 的小型桌面工具，用于联合采集 Keysight N9020A 和 SIGLENT SDS3104X HD 数据，并在采集完成后独立导出客户交付文件和正脉宽分析报告。

项目采用明确的两层数据体系：

- 原始采集：N9020A 保存 CSV；SDS3104X HD 保存 NPZ，并在同一个 NPZ 中保存正脉宽 PWID。
- 离线后处理：按需生成 Scope CSV、CSV/NPZ 波形 PNG、PWID 汇总 CSV 和完全离线的 Plotly HTML 报告。

采集 Worker 不导入 Matplotlib 或 Plotly，不生成 PNG、HTML 或示波器 CSV，也不做批量统计。导出与分析只有在用户手动点击“开始处理”后才会运行。

## 安装与运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

依赖包括 PySide6、PyVISA、NumPy、Matplotlib 和 Plotly。Matplotlib/Plotly 只由离线后处理代码按需导入。

## 原始采集

普通模式使用连续编号保存数据。N9020A Zero Span 扫频模式按频率建立目录：

```text
data/
  600.000MHz/
    000001.csv       # N9020A 原始数据
    000001.npz       # SDS3104X HD 原始波形和元数据
    pulse_width_summary.csv
  605.000MHz/
    000001.csv
    000001.npz
    pulse_width_summary.csv
```

启用示波器时，只有同编号 CSV 和 NPZ 都成功写入，当前编号才会增加。程序先写 `*.csv.tmp` 和 `*.npz.tmp`，两者成功后再提交正式文件；半组失败会清理不完整文件并保留当前编号供下一次重试。关闭示波器时仍兼容原有 CSV-only 采集。

每组联合采集顺序如下：

1. SDS3104X HD 执行 Single 并等待 Trigger Status 为 Stop。
2. 查询本次 Single 的正脉宽；无效时丢弃当前帧并重新执行完整 Single，最多 5 次。
3. 有效时保留成功那次 Single；5 次均无效时保留第 5 次 Single。
4. 采集 N9020A CSV，并读取刚才保留的 SDS3104X HD 波形。
5. 原子提交同编号 CSV/NPZ 文件对。
6. 仅在正式 CSV/NPZ 都存在后，按 index 更新当前目录的 `pulse_width_summary.csv`。

主界面中的“启用 N9020A Zero Span 扫频采集”仅控制 N9020A 扫频模式，不会与 SDS3104X HD 时域波形混淆。

## SDS3104X HD 正脉宽

示波器连接成功后，以及正式采集任务开始前，程序都会发送：

```text
MEAS:ADV:P1:TYPE PWID
```

这会把高级测量 P1 配置为水平测量中的正脉宽。每次 Single 停止后立即发送：

```text
MEAS:ADV:P1:VAL?
```

有效响应按 second 解析，例如 `1.3447E-07` 保存为 `1.3447e-07`。空响应、`---`、包含 `*` 的响应，以及任何不能转换为有限浮点数的内容，都会记录 warning，并触发一次新的完整 Single，而不是在同一帧重复查询 `VAL?`。默认最多执行 5 次 Single。

一旦某次 PWID 有效，后续 `read_waveform()` 读取该次 Single 的波形。5 次全部无效时，PWID 保存为 `NaN`，同时保存第 5 次 Single 的波形，该组仍可正常完成。VISA timeout、connection reset 等查询通信异常不会被当作普通无效值重试，而是继续走设备错误流程。采集停止标志会在每次 Single 前后及后续读取/保存前检查，不使用 `QThread.terminate()`。

GUI 使用统一的 `format_time_value()` 显示，例如：

- `1.3447e-7` → `134.47 ns`
- `2.3e-6` → `2.30 μs`
- `0.0012` → `1.20 ms`
- `NaN` → `N/A`

NPZ 中的权威值始终使用 second，不保存换算后的 ns/μs。

## SDS3104X HD NPZ 字段

NPZ 使用 `numpy.savez_compressed()` 保存：

- `index`：采集编号，`int32`
- `time_s`：时间轴，`float64`，单位 second
- `voltage_v`：电压，`float32`，单位 volt
- `adc`：原始 ADC，`int16`
- `positive_pulse_width_s`：正脉宽，`float64`，单位 second；不可用时为 `NaN`
- `positive_pulse_width_raw`：仪表原始响应，Unicode string，例如 `1.3447E-07` 或 `****`
- `positive_pulse_width_valid`：是否为有效有限值，`bool`
- `positive_pulse_width_attempts`：该样本实际执行的 Single 次数，`int32`
- `device`、`ip`、`channel`、`point_count`
- `adc_bit`、`vdiv`、`offset`、`probe`、`code_per_div`
- `interval`、`sample_rate`、`tdiv`、`delay`

NPZ 是示波器的唯一原始权威格式。相较同时写出数百万行 CSV，它写盘更快、文件更紧凑、数值精度更完整，并适合后续 FFT、STFT 和机器学习处理。Scope CSV 仅作为客户交付格式离线生成。

## 数据导出与分析

主界面底部的“数据导出与分析”区域与采集按钮和采集线程完全独立：

1. 选择原始数据根目录，例如 `D:\data`。
2. 输出目录默认设为 `D:\data\export`，也可另选目录。
3. 选择“全部频点”或“当前目录”。
4. 按需勾选一个或多个输出：Scope CSV、两类 PNG、重建 PWID 汇总 CSV、PWID HTML 报告。
5. 点击“开始处理”。默认不覆盖已有输出；勾选“覆盖已有输出”后才会重建文件。

所有选项默认不勾选。勾选“重建正脉宽汇总 CSV”会以 NPZ 为权威来源重新生成各数据目录的汇总文件；它不受普通导出文件的 overwrite 跳过规则影响。批量任务在独立 `QThread` 中运行，单文件失败会记录日志并继续；点击“停止”后会完成当前文件，再安全退出，不会调用 `QThread.terminate()`。

“全部频点”只扫描数据根目录下一层名称类似 `600MHz`、`600.0MHz`、`600.000MHz` 的目录，忽略 `export`、`images`、`reports` 等非频率目录，也不会递归扫描自己生成的导出内容。“当前目录”直接处理所选目录中的数字编号 CSV/NPZ。

默认输出结构如下：

```text
data/
  600.000MHz/
    000001.csv
    000001.npz
    pulse_width_summary.csv
  export/
    scope_csv/
      600.000MHz/
        000001_scope.csv
    images/
      600.000MHz/
        000001_csv.png
        000001_npz.png
    summary/
      pulse_width_all.csv
      pulse_width_by_frequency.csv
    reports/
      pulse_width_report.html
```

除每个采集目录必需的轻量 `pulse_width_summary.csv` 外，Scope CSV、PNG、总汇总和 HTML 都保存在 `export/`。CSV/NPZ 通过数字 stem 配对；只有正式 CSV/NPZ 文件对才会进入频点级 summary。

### Scope NPZ → CSV

输出文件名使用 `000001_scope.csv`，避免与 N9020A 的 `000001.csv` 混淆。列固定为：

```text
index,time_s,voltage_v,adc
```

转换按数据块写入，适合大波形。正脉宽不会在每一行重复，而是放入轻量汇总 CSV。

### CSV / NPZ → PNG

- N9020A CSV 输出 `000001_csv.png`。程序会跳过仪表 metadata/header，优先识别 Frequency/Time 和 Amplitude/Power 数值列，分别支持 Spectrum 与 Zero Span。
- Scope NPZ 输出 `000001_npz.png`，绘制 Time/Voltage，并显示 Channel、Sample Rate、Points 和 `Positive Pulse Width: 134.47 ns`；旧 NPZ 或无效值显示 `Positive Pulse Width: N/A`。

图片统一为 12×6 inch、150 DPI 的 PNG。大波形使用最多 100,000 点的 min/max envelope 预览降采样，以尽量保留窄脉冲峰值；原始 NPZ 不会被修改。

### 正脉宽汇总 CSV

每个 Zero Span 频率目录直接保存 `<频点>/pulse_width_summary.csv`；普通非扫频模式则保存 `<data_root>/pulse_width_summary.csv`。字段固定为：

```text
index,pulse_width_s,pulse_width_ns,valid,attempts,raw_value,npz_file,csv_file
```

采集时只在 CSV/NPZ 正式提交成功后更新。更新采用小型 index 映射和原子重写，相同 index 使用最新正式数据替换，不会产生重复行，同时会移除文件对已不存在的旧记录。损坏或旧格式 summary 会从正式 NPZ/CSV 文件对重建。

旧 NPZ 只有 `positive_pulse_width_s` 时，会推导 valid，attempts 使用 1，raw_value 使用对应数值字符串；完全没有 PWID 字段时使用 `NaN`、valid=0、attempts=0 和空 raw_value。

`export/summary/pulse_width_all.csv` 合并全部频点，`export/summary/pulse_width_by_frequency.csv` 保存统计结果。生成 HTML 时优先读取已有频点级 summary；summary 不存在时才扫描 NPZ 并自动建立它。用户主动勾选“重建正脉宽汇总 CSV”时会强制以 NPZ 重建。

`export/summary/pulse_width_by_frequency.csv` 保存每个频点的：

- 总样本数、有效/无效样本数、有效率
- mean、median、population std、min、max、P05、P95（单位 ns）
- 缺失 CSV / NPZ 数量

所有统计排除 `NaN`，但总数和无效数会保留。

### 完全离线 HTML 报告

`export/reports/pulse_width_report.html` 是单个自包含文件，Plotly JavaScript 内联写入，不依赖 CDN 或互联网，也不要求客户安装 Python。Chrome/Edge 直接打开即可使用 Hover、Zoom、Pan、Legend 和 Reset Axes。

报告包含：

- 数据根目录、频率范围、频点数、总样本数、有效/无效数和有效率
- 每频点 mean/median/std/min/max/P05/P95 表格
- 可切换频点的正脉宽直方图
- 样本序号散点图，hover 显示频率、编号、PWID 和原始 NPZ/CSV 路径
- 全频率箱线图
- 各频率 mean/median 趋势图

## 测试

```powershell
python -m compileall .
python -m pytest
```

自动测试覆盖 1/2/4/5 次完整 Single、通信异常不重试、停止检查、最终波形对应最后一次 Single、CSV/NPZ 原子配对、NPZ 新字段、summary 创建/去重/缺失文件对/旧 NPZ 重建、普通与频点目录、HTML 优先读取 summary，以及已有绘图和离线报告功能。真实 N9020A/SDS3104X HD 的网络、SCPI 兼容性、Trigger Stop 时序和长时间连续采集仍需连接真实仪表验证。
