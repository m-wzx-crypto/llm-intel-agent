"""信源清单：按业务分组，缺失的组显式标出原因（缺席 ≠ 没有）。

实测可用（2026-11-23 00:13）：
  ✅ 量子位 / IT之家 / 钛媒体 / 极客公园 / InfoQ / 雷锋网 / 36氪(www) / 爱范儿 / 少数派
  ❌ 机器之心(rss 已废) / 虎嗅 / RSSHub(被墙) / arXiv(格式变) / 品玩 / 界面 / 亿欧 / FT中文

公众号：RSS 镜像通道。公共实例需具体 feed id，因此设计为**用户可配置**：
  在设置里粘贴 wechat2rss / we-mp-rss 的 feed URL，或付费 RSS 服务生成的 URL。
  未配置时状态 = unavailable，error 写明「未配置订阅源」，绝不当成「没有新闻」。
小红书：默认 off（合规决策，见 docs 技术方案 §9），状态 = unavailable + 说明。
"""
from typing import Any

# ── 分组：zhipu 智谱专题 / tech 行业科技 / business 商业财经 / wechat 公众号 / research 研报 / competitor 竞对 ──
SOURCES: list[dict[str, Any]] = [
    # 智谱专题：媒体面按关键词检索，官网新闻作一手发言（放最前，保证正文抓取预算优先给专题）
    {"id": "em_search_zhipu", "name": "智谱舆情(东财搜索)", "kind": "api",
     "url": "em:search:zhipu", "group": "zhipu", "tier": 2},
    {"id": "em_search_tangjie", "name": "唐杰舆情(东财搜索)", "kind": "api",
     "url": "em:search:tangjie", "group": "zhipu", "tier": 2},
    # 唐杰单独检索命中太少：补一条「唐杰+智谱」的宽口径（不限标题）作为召回兜底
    {"id": "em_search_tangjie_wide", "name": "唐杰舆情(宽口径)", "kind": "api",
     "url": "em:search:tangjie_wide", "group": "zhipu", "tier": 2},
    {"id": "zhipu_news", "name": "智谱官网新闻", "kind": "api",
     "url": "zhipu:news", "group": "zhipu", "tier": 1},
    # 行业科技
    {"id": "qbitai",   "name": "量子位",    "url": "https://www.qbitai.com/feed",    "group": "tech", "tier": 2},
    {"id": "leiphone", "name": "雷锋网",    "url": "https://www.leiphone.com/feed",  "group": "tech", "tier": 2},
    {"id": "geekpark", "name": "极客公园",  "url": "https://www.geekpark.net/rss",   "group": "tech", "tier": 2},
    {"id": "infoq",    "name": "InfoQ",    "url": "https://www.infoq.cn/feed",      "group": "tech", "tier": 3},
    {"id": "ifanr",    "name": "爱范儿",    "url": "https://www.ifanr.com/feed",     "group": "tech", "tier": 3},
    {"id": "sspai",    "name": "少数派",    "url": "https://sspai.com/feed",         "group": "tech", "tier": 3},
    # 商业财经
    {"id": "36kr",     "name": "36氪",     "url": "https://www.36kr.com/feed",      "group": "business", "tier": 2},
    {"id": "tmtpost",  "name": "钛媒体",    "url": "https://www.tmtpost.com/rss.xml","group": "business", "tier": 2},
    {"id": "ithome",   "name": "IT之家",    "url": "https://www.ithome.com/rss/",    "group": "business", "tier": 2},
    # 海外交易流：只收「AI + 交易」条目（融资/收购/订单）
    {"id": "techcrunch", "name": "TechCrunch", "url": "https://techcrunch.com/feed/",
     "group": "business", "tier": 2, "filter": "ai_deal"},
    {"id": "crunchbase_news", "name": "Crunchbase News", "url": "https://news.crunchbase.com/feed/",
     "group": "business", "tier": 2, "filter": "ai_deal"},
    {"id": "venturebeat", "name": "VentureBeat", "url": "https://venturebeat.com/feed/",
     "group": "tech", "tier": 2, "filter": "ai_deal"},
    {"id": "cyzone",   "name": "创业邦",    "url": "https://www.cyzone.cn/rss/",
     "group": "business", "tier": 2, "filter": "ai_deal"},
    {"id": "siliconangle", "name": "SiliconANGLE", "url": "https://siliconangle.com/feed/",
     "group": "tech", "tier": 2, "filter": "ai_deal"},
    {"id": "verge_ai", "name": "The Verge AI",
     "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
     "group": "tech", "tier": 3, "filter": "ai_deal"},

    # 百度：资讯搜索（关键词）+ 热搜榜（公开榜单，无需登录）
    {"id": "baidu_news_zhipu", "name": "百度资讯·智谱", "kind": "api",
     "url": "baidu:news:zhipu", "group": "business", "tier": 2},
    {"id": "baidu_news_tangjie", "name": "百度资讯·唐杰", "kind": "api",
     "url": "baidu:news:tangjie", "group": "business", "tier": 2},
    {"id": "baidu_hot", "name": "百度热搜", "kind": "api",
     "url": "baidu:hot", "group": "business", "tier": 3},
    # JSON API 型：给的是结构化事实（研报/公告/快讯），不是新闻
    {"id": "eastmoney_report", "name": "券商研报", "kind": "api", "url": "eastmoney:report",
     "group": "research", "tier": 1},
    {"id": "cninfo_notice",    "name": "上市公司公告", "kind": "api", "url": "cninfo:notice",
     "group": "competitor", "tier": 1},
    {"id": "sina_7x24",        "name": "新浪财经7x24", "kind": "api", "url": "sina:7x24",
     "group": "business", "tier": 1},
    {"id": "em_kuaixun",       "name": "东财快讯", "kind": "api", "url": "em:kuaixun",
     "group": "business", "tier": 1},
    {"id": "ths_7x24",         "name": "同花顺7x24", "kind": "api", "url": "ths:7x24",
     "group": "business", "tier": 1},
    {"id": "wscn_lives",       "name": "见闻快讯", "kind": "api", "url": "wscn:lives",
     "group": "business", "tier": 1},
    {"id": "em_announcement",  "name": "东财公告全文", "kind": "api", "url": "em:ann",
     "group": "competitor", "tier": 1},
    {"id": "sec_deals",        "name": "美股交易披露(SEC)", "kind": "api", "url": "sec:deals",
     "group": "research", "tier": 1},
    {"id": "sec_edgar",        "name": "美股披露(SEC)", "kind": "api", "url": "sec:filings",
     "group": "research", "tier": 1},
    {"id": "blackrock_cn",     "name": "贝莱德投资观点", "kind": "api", "url": "blackrock:cn",
     "group": "research", "tier": 1},
    {"id": "goldman_insights", "name": "高盛观点", "kind": "api", "url": "goldman:insights",
     "group": "research", "tier": 1},
]

# 需要用户配置才有内容的通道（配置后自动并入 SOURCES）
CONFIGURABLE = [
    {
        "id": "wechat_rss", "name": "公众号 RSS", "group": "wechat",
        "why": "微信没有公开时间线，RSS 镜像需要逐个号的 feed 地址",
        "how": "设置里粘贴 wechat2rss / we-mp-rss / 付费 RSS 服务生成的 feed URL（每行一个）",
    },
    {
        "id": "xiaohongshu", "name": "小红书", "group": "social",
        "why": "直连需登录态与签名，有判例风险，默认关闭（技术方案 §9）",
        "how": "如需开启，走「客户本机扫码只读」或商业数据源对接",
    },
    {
        "id": "weibo", "name": "微博", "group": "social",
        "why": "关键词搜索必须登录；匿名只读接口已不可用（实测 432/403）",
        "how": "信源页点「微博」卡片，用微博 App 扫码登录本机只读通道；登录态只存本机 Keychain",
    },
]

# 实体词表：命中即加权并归入板块（key 必须与 CATEGORIES 完全一致）
ENTITIES = {
    "智谱·唐杰": ["智谱", "GLM", "ChatGLM", "Zhipu", "唐杰", "张鹏"],
    "竞对动态": ["月之暗面", "Kimi", "MiniMax", "百川", "零一万物", "阶跃", "DeepSeek", "深度求索",
               "通义", "Qwen", "豆包", "文心", "混元", "科大讯飞", "商汤", "腾讯", "阿里", "字节",
               "Anthropic", "OpenAI", "谷歌", "Gemini", "Llama", "Meta"],
    "具身智能": ["宇树", "Unitree", "智元", "Agibot", "银河通用", "Galbot", "人形机器人", "具身智能",
               "Figure", "1X", "星动纪元", "星海图", "众擎"],
    "算力供应链": ["算力", "GPU", "H100", "H200", "B200", "Blackwell", "昇腾", "英伟达", "NVIDIA",
                "台积电", "中芯", "寒武纪", "摩尔线程", "断供", "缺卡", "机柜", "数据中心", "液冷",
                "光模块", "HBM", "芯片", "产能"],
    "资本事件": ["融资", "IPO", "估值", "收购", "合并", "裁员", "倒闭", "破产", "上市", "轮融资",
              "亿元", "亿美元"],
}

EVENT_WORDS = {
    "发布": ["发布", "推出", "上线", "开源", "更新", "升级", "首发", "亮相"],
    "适配": ["适配", "支持", "接入", "兼容", "迁移", "合作"],
    "采购": ["采购", "订单", "中标", "签约", "供货", "订购", "租用"],
    "延期": ["延期", "推迟", "跳票", "取消", "暂停", "搁置", "下架"],
    "人事": ["离职", "加入", "任命", "裁员", "招聘", "扩招", "返聘"],
    "资本": ["融资", "IPO", "估值", "收购", "破产", "亏损", "盈利", "冲刺"],
}

CATEGORIES = ["智谱·唐杰", "竞对动态", "具身智能", "算力供应链", "资本事件", "行业其他"]
CATEGORY_PRIORITY = {c: i for i, c in enumerate(CATEGORIES)}
