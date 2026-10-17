# 返场投票 · 票数监控台

王者荣耀周年庆「限定皮肤返场投票」的实时票数抓取与排名看板。
展示每款参选皮肤的**名次、票数、与上一名的差距**，并标出返场线。

**在线看板（GitHub Pages，手机电脑随时打开，不用开电脑）：**
<https://dusty-lwy.github.io/portfolio/vote-dashboard/>

---

## 一、它怎么工作（新架构）

```
GitHub Actions（每 10 分钟定时）
        │  运行 fetcher.py
        ▼
  抓取腾讯 AMS 活动接口 → data/latest.json + data/history.json + data/status.json
        │  有变化才提交（票数没动就不会触发提交和 Pages 重建）
        ▼
GitHub Pages 静态托管 → index.html 读取 JSON 渲染看板
```

- **不依赖常驻服务**：没有 `server.py`，电脑可以关机。
- **数据自动更新**：票数变化后 10 分钟内看板就能看到新数据。
- **手机可用**：直接收藏上面的在线地址即可。
- **健壮**：抓取失败自动重试 3 次、超时 15 秒、异常全部写进 `data/status.json` 并在页面上显示原因。

## 二、本地跑

```bash
cd 返场投票监控
python -m http.server 8787        # 打开 http://127.0.0.1:8787
python fetcher.py                 # 手动抓取一次（写入 data/）
python fetcher.py --import "妲己 愿照·众生和 18910170
司空震 愿照·山河定 12701488"      # 兜底：手动导入一条快照
```

## 三、数据从哪来（重点）

投票是**游戏内活动**，官方没有提供免登录的公开票数网页。票数由腾讯的活动系统接口返回：

```
https://smoba.ams.game.qq.com/ams/ame/amesvr     （POST）
```

返回的 JSON 里，`modRet.sOutValue2` 是一串皮肤编号（`v_1|v_2|v_3|...`），
`modRet.sOutValue3` 是对应的票数（`123|456|789`），两者**按下标一一对应**。

**关键**：每一次活动的 `iActivityId`、`iFlowId`、`g_tk` 都不一样，所以不能写死。
这三个参数写在 `数据源配置.json` 里，抓取器每次运行都会读取。

### 拿到当期参数的方法

1. 手机连电脑代理（Charles / Fiddler / HttpCanary 都行），或用电脑开一个模拟器登游戏。
2. 进入游戏内活动 **周年庆 → 返场投票 → 限定皮肤返场投票**，翻一下排行榜。
3. 在抓包记录里找到发往 `smoba.ams.game.qq.com/ams/ame/amesvr` 的请求，
   把 URL 和表单里的 `iActivityId`、`iFlowId`、`g_tk` 抄下来。
4. 填进仓库里的 `数据源配置.json`（GitHub 网页上直接编辑并提交，或本地改完推上去），
   下一次定时抓取（10 分钟内）就会生效；也可以在仓库 Actions 页点 **Run workflow** 立即触发一次。

### 编号 → 英雄/皮肤 的映射

接口返回的是 `v_1` 这类编号。抓包时页面上会显示编号对应的皮肤，
按 `v_1 = 妲己|愿照·众生和` 的格式填进 `数据源配置.json` 的 `hero_map` 里。
不填也能看，但列表里显示的会是编号本身。

### 抓不到怎么办：手动导入

`fetcher.py --import` 是兜底通道，两种格式都认：

```
妲己 愿照·众生和 18910170
司空震 愿照·山河定 12701488
```

或者直接粘两行编号/票数串：

```
v_1|v_2|v_3
123456|78901|5000
```

导入后把 `data/` 目录提交上去，看板立即显示。

## 四、目录

| 文件 | 作用 |
| --- | --- |
| `index.html` | 前端看板（单文件，无构建，直接读 data/*.json） |
| `fetcher.py` | 抓取器（本地 / GitHub Actions 通用） |
| `数据源配置.json` | 数据源参数与编号映射 |
| `data/latest.json` | 最新一次快照 |
| `data/history.json` | 历史快照（最多 500 条，趋势图数据源） |
| `data/status.json` | 抓取状态与错误说明（看板「数据状态」页展示） |
| `.github/workflows/fetch-votes.yml` | 定时抓取工作流（每 10 分钟） |
| `_legacy/` | 旧版 `server.py` / `投票监控台.html` / `votes.db`，已不再使用 |

## 五、已知限制与注意

- 参数靠抓包取得，**没有抓包就拿不到实时数据**，此时只能用手动导入。活动结束后接口会失效。
- `g_tk` 是活动会话令牌，有效期短；建议活动结束后清空 `数据源配置.json` 里的值再提交。
- 抓取的是公开的活动排行榜票数，**请自用、低频轮询**（默认 10 分钟一次），不要用于商业用途或高频压对方接口。
- 看板数据每分钟自动刷新；GitHub Actions 每 10 分钟抓一次。