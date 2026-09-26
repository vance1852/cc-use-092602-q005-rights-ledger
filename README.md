# 实现家庭土地权益额度账本基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rights_ledger/`：按家庭隔离的土地权益额度账本，记录权益来源、适用地类、有效期、冻结与实际消耗，分配确认时按规则选择额度并与地块预留原子落账，超额申请进入有期限的复核队列；
- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 家庭土地权益额度账本规则

- 权益额度按家庭隔离，逐项记录来源（承包地合并、宅基地资格、搬迁奖励、政策调整、复核授予）、适用地类、有效期、冻结与实际消耗，全部变动写入带余额的流水；
- 跨年度项目按可注入时钟把预计占用切分到各日历年度；分配确认时在同一事务内重新选择可用额度（先到期先消耗或按来源优先级）、冻结权益并预留地块，过期或被他户冻结的额度会在确认时暴露为明确冲突；
- 交付把冻结转为实际消耗；退出或失败只返还尚未交付的冻结部分；
- 超过可用额度的申请进入有期限的复核队列，复核人不得批准自己提交的例外，批准后由系统授予等额例外权益；
- 年度结算核销过期权益并关闭该年度：规则版本不能回写已结算年度，新的预计占用也不得落入已结算年度；
- 家庭角色只能查看本户汇总与本户流水，产权管理与审计角色可通过解释接口还原每次扣减、返还和超额决定的规则版本、经办人与复核人。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rights_ledger.acceptance --workspace .
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
```

四条命令使用临时 SQLite 数据库完成家庭权益额度授予、跨年度申请分配、超额复核、年度结算、村镇与地块登记、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rights_ledger.api --database ledger.sqlite3 --host 127.0.0.1 --port 8083
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
