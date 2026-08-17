SDS3104X HD DELAY 测量延时稳定性测试工具
========================================

用途
----
测试 SDS3104X HD Single 完成后，等待多少 ms 再读取
MEAS:ADV:P1:VAL? 最稳定。工具会主动配置：

MEAS:ADV:P1:TYPE DELAY

默认测试延时
------------
0、50、100、150、200、300、500 ms

使用步骤
--------
1. PC 与 SDS3104X HD 网络连接正常。
2. 确认 config.json 中 scope.ip 和 scope.channel 正确。
3. 示波器提前配置好信号和 Trigger。
4. 运行 delay-measurement-tester.exe。
5. 等待所有测试完成。
6. 查看 output 目录中的测试结果 CSV。
7. 找到成功率 >= 99% 的最短 settle delay。
8. 修改 config.json：scope.delay_settle_delay_ms。

也可以在 PowerShell 中指定参数：

  .\delay-measurement-tester.exe --ip 192.168.1.50
  .\delay-measurement-tester.exe --samples 20
  .\delay-measurement-tester.exe --delays 50,100,150,200

测试结果说明
------------
VALID：正常获得 DELAY。
INVALID：MEAS:ADV:P1:VAL? 返回 ****、---、空字符串等无效结果。

汇总结果：output\delay_measurement_test_results.csv
详细记录：output\delay_measurement_test_details.csv

注意
----
测试工具只测试 DELAY，不读取完整波形，不保存 NPZ，不访问 N9020A。
