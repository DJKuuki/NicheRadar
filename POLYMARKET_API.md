# Polymarket API Notes

基于官方文档整理：

- 文档入口：`https://docs.polymarket.com/api-reference/introduction`
- `Gamma API`：`https://gamma-api.polymarket.com`
- `Data API`：`https://data-api.polymarket.com`
- `CLOB API`：`https://clob.polymarket.com`

## 当前机器人实际使用的接口

### 1. 市场发现

`GET https://gamma-api.polymarket.com/markets`

用途：

- 拉取市场列表
- 获取问题文本、描述、截止时间、成交量
- 获取 `clobTokenIds`
- 获取 `bestBid` / `bestAsk` 等字段

当前最小参数：

- `limit`
- `closed`

示例：

```powershell
Invoke-RestMethod -Uri "https://gamma-api.polymarket.com/markets?limit=20&closed=false"
```

### 2. 盘口读取

`GET https://clob.polymarket.com/book?token_id=<TOKEN_ID>`

用途：

- 读取某个 outcome token 的盘口
- 获取 bids / asks
- 获取 `last_trade_price`
- 获取 `tick_size`

示例：

```powershell
Invoke-RestMethod -Uri "https://clob.polymarket.com/book?token_id=98022490269692409998126496127597032490334070080325855126491859374983463996227"
```

## 认证边界

根据官方文档：

- `Gamma API` 公开，无需认证
- `Data API` 公开，无需认证
- `CLOB API` 的读接口公开，无需认证
- `CLOB API` 的交易接口需要认证

所以当前影子盘机器人只用公开读接口，不需要钱包私钥或 API key。

## 下一步接口

如果要继续推进，优先补这些接口：

- `GET /markets` 的分页与过滤
- `GET /book`
- `GET /spread`
- `GET /midpoint`
- `GET /prices-history`

如果要进入真实下单，再补：

- `POST /order`
- `DELETE /order`
- 认证流程

## 当前实现状态

当前代码已经实现：

- 用 `Gamma API` 拉市场
- 用 `CLOB API /book` 拉 YES token 盘口
- 用外部 `RSS/Atom` feed 作为证据源补充市场打分

外部证据源不是 Polymarket 官方接口的一部分，需要单独维护在 `data/evidence_sources.json`。
