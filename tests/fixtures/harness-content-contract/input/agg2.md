回答每一条时，必须在 reply 或 未决 的正文里带上它的 ID（形如 AGG1）；未解决的条目要原样带 ID 进 未决[]。

AGG1 | 多文件聚合任务：汇总 run 内相对路径 input/agg-data/ 下 6 个月度数据文件 | 派执行叶：用 Bash + Python 读全部 6 个文件（month-01.txt 至 month-06.txt，均在 run 内 input/agg-data/ 下），每文件每行一个整数；计算①每个文件的行数与和；②六个文件总行数与总和；③全部数字的最大值与最小值。把结果写成两份产物：product/agg.json（JSON，键 files{文件名:{lines,sum}}、total_lines、total_sum、max、min）与 doc/agg_report.md（写明方法、逐文件数据、第二种独立算法或逐行复核的结果与产物路径）。JSON 数值必须与文件真实内容一致。
