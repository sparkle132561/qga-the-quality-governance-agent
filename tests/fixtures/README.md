# 公开测试夹具

- `extraction-boundaries.json`：6 条最小化、改写公司/品牌/型号的解析输入，覆盖品牌后缀、货号、未来协议、供货对象和空关系。只保留测试实际读取的字段，不含请求、费用、运行路径或完整公告。
- `rewrite-fence.json`：自建 JSON fence 回答，验证查询改写的解析和型号保留。
- `task05-c-midea-range-excerpt.txt`：经品牌/型号脱敏的多生产区间表格片段。文件名保留测试来源标识，不代表当前公开数据集。
- `contract-cases.json`：40 条自建机制边界用例，`human_verified_count=0`，不能据此宣称模型业务正确率。
- `pricing-snapshot.json`：固定测试价目快照，只验证 token 计量和计算公式；不代表当前报价或真实扣费。

这些输入用于离线回归，不是可重新统计历史模型质量的实验记录。历史整轮日志和实验数据仍留在原项目，没有随公开包分发。
