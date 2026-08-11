# N9020A + SDS3104X HD 联合采集工具

基于 **PySide6** 的桌面应用，用于连续采集 Keysight N9020A CSV，并可为每份 CSV 配对保存一份 SIGLENT SDS3104X HD 示波器 NPZ 波形。

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
- 可随时中断。
- 程序退出并重新打开后，可继续从上次编号采集（状态持久化）。

## 安装
```bash
python -m venv .venv
source .venv/bin/activate
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

## NPZ 内容

`numpy.savez_compressed()` 文件包含 `index`、`time_s`、`voltage_v`、`adc`，以及设备、IP、通道、点数、ADC 位数、垂直/时间刻度与采样率等元数据。

## 测试

```bash
python -m compileall .
python -m pytest
```
