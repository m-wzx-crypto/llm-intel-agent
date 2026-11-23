"""预置公众号：公开 Wechat2RSS 源里，与「AI 产业链 / 融资 / 算力 / 具身」相关的账号。

来源 https://wechat2rss.xlab.app/list/all/（公开服务，无需鉴权，平均 6 小时更新）。
收录前逐个实测过：HTTP 200、20 条最新文章、发布时间在 7 天内；停更的号一律不进清单
（浅黑科技停在 2026-06、机器学习初学者停在 2025-08）。

``on`` = 首次安装默认订阅；用户可在「信源」页一键点掉。
"""

PRESETS = [
    # ── 默认订阅 ──────────────────────────────────────────────
    {"name": "机器之心", "on": True, "cat": "AI 媒体",
     "note": "融资 / 发布 / 模型进展，行业面最全",
     "feed": "https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml"},
    {"name": "量子位", "on": True, "cat": "AI 媒体",
     "note": "产品与算力动态，报道频次高",
     "feed": "https://wechat2rss.xlab.app/feed/7131b577c61365cb47e81000738c10d872685908.xml"},
    {"name": "新智元", "on": True, "cat": "AI 媒体",
     "note": "产业与海外动向",
     "feed": "https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"},
    {"name": "夕小瑶科技说", "on": True, "cat": "AI 媒体",
     "note": "行业解读，GLM 相关内容较多",
     "feed": "https://wechat2rss.xlab.app/feed/a1cd365aa14ed7d64cabfc8aa086da40ecaba34d.xml"},
    {"name": "42章经", "on": True, "cat": "创投",
     "note": "创投访谈；实测「智谱」出现频率最高",
     "feed": "https://wechat2rss.xlab.app/feed/31436fcc3bba8c2c2a9337a163afcb3b5a57a0a0.xml"},
    {"name": "极客公园", "on": True, "cat": "科技商业",
     "note": "科技商业报道，融资与芯片线索多",
     "feed": "https://wechat2rss.xlab.app/feed/1a5aec98e71c707c8ca092bc2c255b9d4bac477d.xml"},

    # ── 可选（默认不订阅，偏学术/教程，噪声大） ────────────────
    {"name": "PaperWeekly", "on": False, "cat": "学术",
     "note": "论文与模型前沿，产业信号少",
     "feed": "https://wechat2rss.xlab.app/feed/3be891c2f4e526629ab055a297cc2cd6c1f0a563.xml"},
    {"name": "集智俱乐部", "on": False, "cat": "学术",
     "note": "复杂系统 / AI 学术向",
     "feed": "https://wechat2rss.xlab.app/feed/8540570d27c0bfe0a219173cf1ace83ae79445cb.xml"},
    {"name": "我爱计算机视觉", "on": False, "cat": "技术",
     "note": "CV 方向工程与论文",
     "feed": "https://wechat2rss.xlab.app/feed/b81ffcfff1107b5265cd7e39de610dc7ca72caf4.xml"},
    {"name": "Datawhale", "on": False, "cat": "技术",
     "note": "开源与教程，偏学习内容",
     "feed": "https://wechat2rss.xlab.app/feed/4d620d988cb21cfeefd2263207221f0dc70df9ff.xml"},
    {"name": "傅盛", "on": False, "cat": "个人",
     "note": "从业者个人号，观点多于事实",
     "feed": "https://wechat2rss.xlab.app/feed/71257003d43d39b91ff9d38b6f3330c883dc8e0c.xml"},
]

_BY_FEED = {p["feed"]: p for p in PRESETS}


def default_feeds() -> list:
    """首次安装默认订阅的 feed 列表"""
    return [p["feed"] for p in PRESETS if p.get("on")]


def name_of(feed: str) -> str:
    """feed → 公众号名；不在清单里的自定义 feed 返回空串"""
    p = _BY_FEED.get((feed or "").strip())
    return p["name"] if p else ""


def catalog(feeds: list) -> list:
    """给前端的清单：带上当前是否已订阅"""
    cur = set((feeds or []))
    out = []
    for p in PRESETS:
        out.append({"name": p["name"], "feed": p["feed"], "cat": p["cat"],
                    "note": p["note"], "on": p["feed"] in cur})
    return out
