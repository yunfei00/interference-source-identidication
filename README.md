# N9020A CSV 采集工具

基于 **PySide6** 的桌面应用，用于连接 Keysight N9020A 仪表并定时采集 CSV 数据。

## 功能
- 手动填写仪表地址（VISA Resource，例如 `TCPIP0::192.168.1.100::inst0::INSTR`）。
- 连接/断开仪表。
- 采集前配置输出文件夹。
- 支持设置采集间隔、目标总数。
- 文件编号从 `000001.csv` 开始，自动跳过已存在编号。
- 实时显示当前进度。
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
