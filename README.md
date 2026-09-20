# 美术馆文创授权履约系统

本项目用于美术馆管理作品授权、产品开发、供应履约和多渠道销售之间的联系。现有工程提供 HTTP 服务入口、SQLite 基础连接、权利范围示例和领域说明，便于后续形成清晰的授权审查与商品追溯能力。

## 使用方式

```bash
PYTHONPATH=src python -m museum_merch.main
```

运行 `PYTHONPATH=src python -m unittest discover -s tests` 可执行基础测试。服务的健康检查地址为 `GET /health`。

## 资料目录

- `docs/rights.md`：作品权利与履约边界。
- `fixtures/license-scope.json`：授权范围示例。
