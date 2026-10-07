# 演示日志包

[下载 demo-logs.zip](demo-logs.zip)。文件由仓库根目录 `demo.py` 生成，全部为虚构日志，不含用户上传的原始或脱敏附件。

包内包含 2 个 Node、2 个 Pod、8 份日志文件。上传后确认目录并建立索引，可按根目录 [README](../../README.md#3-用演示包完整查一次-http-500) 的示例验证接口错误、流水号和历史 GZIP 搜索。

重新生成：

```bash
python demo.py --output docs/examples/demo-logs.zip
```

原包中日志时间固定为 2026-09-08。第一次试用可不填时间筛选；若填写，使用该日期与 UTC+08:00。
