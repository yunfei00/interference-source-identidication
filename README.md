# N9020A + SDS3104X HD 联合采集工具

基于 **PySide6** 的桌面应用，用于连续采集 Keysight N9020A CSV，并可为每份 CSV 配对保存一份 SIGLENT SDS3104X HD 示波器 NPZ 波形。采集完成后，可使用独立的离线转换功能批量生成 PNG。

## 功能
- 手动填写仪表地址（VISA Resource，例如 `TCPIP0::192.168.1.100::inst0::INSTR`）。
- 连接/断开仪表。
- 独立连接/断开 SDS3104X HD，配置 IP、通道和 Single 超时。
- 采集前配置输出文件夹。
- 支持设置采集间隔、目标总数。
- 普通模式保存同编号的 `000001.csv` 和 `000001.npz`。
- N9020A Zero Span 扫频模式保持 `{频率:.3f}MHz/000001.*` 目录结构。
- 示波器关闭时维持原 CSV-only 行为；开启时只有 CSV + NPZ 都存在才视为完成。
- 单组采集在工作线程执行，完成后再等待设定间隔，不会重叠采集。
- 实时显示当前进度。
- 显示最近一帧示波器点数、电压范围、读取与保存耗时。
- 每次 SDS3104X HD Single 完成后读取高级测量 P1 正脉宽并写入 NPZ。
- 独立支持单个文件或递归目录的 N9020A CSV / SDS NPZ 转 PNG。
- 大波形绘图使用 min/max envelope 预览降采样，原始 NPZ 不受影响。
- CSV 与 NPZ 分别输出为 `000001_csv.png` 和 `000001_npz.png`，不会冲突。
- 默认跳过已有图片，也可手动启用覆盖。
- 可随时中断。
- 程序退出并重新打开后，可继续从上次编号采集（状态持久化）。

## 安装
```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 运行
```bash
python app.py
```

## 说明
- 采集命令默认使用 `MMEM:DATA? "D:\\temp.csv"` 作为示例读取流程（部分仪表/固件可能不同）。
- 可根据你的 N9020A 实际 SCPI 命令，在 `n9020a_client.py` 中调整 `fetch_csv_text()`。
- SDS3104X HD 默认 VISA Resource 为 `TCPIP0::192.168.1.50::INSTR`，默认通道为 `C1`。
- 联合采集先写 `*.csv.tmp` / `*.npz.tmp`，两者成功后才提交为正式文件；失败会保留当前编号供重试。

## SDS3104X HD 正脉宽

连接示波器成功后，程序会自动发送：

```text
MEAS:ADV:P1:TYPE PWID
```

正式采集任务开始前会再次发送该命令，确保高级测量 P1 配置为正脉宽。每组采集顺序为：

1. SDS3104X HD Single 并等待 Trigger Stop。
2. 发送 `MEAS:ADV:P1:VAL?` 读取本帧正脉宽。
3. 采集 N9020A CSV。
4. 读取 SDS3104X HD 波形并保存 NPZ。

有效值以 second 返回并按 `float64` 保存。空字符串、`---`、包含 `*` 的响应以及其他不能转换为有限浮点数的内容统一记录 warning 并保存为 `NaN`，不会导致本组 CSV/NPZ 采集失败。GUI 会把有效值自动显示为 s、ms、μs、ns 或 ps；无效值显示为 `N/A`。

## NPZ 内容

`numpy.savez_compressed()` 文件包含：

- `index`：采集编号，`int32`。
- `time_s`：时间轴，`float64`，单位 second。
- `voltage_v`：电压，`float32`，单位 volt。
- `adc`：原始 ADC，`int16`。
- `positive_pulse_width_s`：正脉宽，`float64`，单位 second；不可用时为 `NaN`。
- `device`、`ip`、`channel`、`point_count`、`adc_bit`、`vdiv`、`offset`、`probe`、`code_per_div`、`interval`、`sample_rate`、`tdiv`、`delay` 等元数据。

## 数据转图片

主窗口底部的“数据转图片”区域与实时采集完全解耦：采集 Worker 不导入 Matplotlib、不绘图，也不会在采集完成后自动生成图片。只有用户点击“开始转换”时才启动独立图片转换线程。

支持的数据源：

- 单个 `.csv` 文件。
- 单个 `.npz` 文件。
- 目录中的全部 CSV/NPZ，并递归扫描子目录。

选择数据源后，默认输出到原始数据根目录的 `images/`，并保持原目录层级。例如：

```text
data/
  600.000MHz/
    000001.csv
    000001.npz
  images/
    600.000MHz/
      000001_csv.png
      000001_npz.png
```

CSV 转换会查找连续数值数据区，并根据 Frequency/Time 与 Amplitude/Power 等表头信息识别普通频谱或 Zero Span；不能可靠判断的单位不会被猜测。NPZ 转换绘制 Time/Voltage，兼容没有 `positive_pulse_width_s` 的旧 NPZ，并在图中显示：

```text
Positive Pulse Width: 134.47 ns
```

不可用时显示 `Positive Pulse Width: N/A`。对于数百万点波形，绘图预览最多保留 100,000 个 min/max envelope 点，以尽量保留窄脉冲峰值；原始 NPZ 内容不会改变。

批处理中的单文件失败会记录日志并继续；可点击“停止转换”，程序会在当前文件完成后安全停止。默认不覆盖已有 PNG，统计会分别显示 Success、Skipped 和 Failed。

## 测试

```bash
python -m compileall .
python -m pytest
```
