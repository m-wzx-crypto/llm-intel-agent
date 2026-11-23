"""实体消歧：别名 → 规范实体。

必须解决的问题（真实踩过的坑）：
  「北美豆包」「美国豆包」「美版豆包」指的是 **Gemini**，不是字节的豆包。
  子串匹配会把 "北美豆包" 里的 "豆包" 命中成字节豆包 → 归属错误。

解法：**最长匹配优先 + 跨度覆盖**
  1. 所有别名按长度倒序扫描；
  2. 命中后记录其在文本中的跨度；
  3. 任何被更长命中跨度覆盖的短命中直接丢弃；
  4. 剩余命中映射到规范实体 + 板块。

这是技术方案 §3.1 resolve_entity 的第一级（别名表），成本为零且可测。
第二级（向量 ANN）与第三级（LLM 裁决）在事件量上来后再接。
"""
from typing import NamedTuple

# 板块
CAT_ZHIPU = "智谱·唐杰"
CAT_COMPETITOR = "竞对动态"
CAT_EMBODIED = "具身智能"
CAT_COMPUTE = "算力供应链"
CAT_CAPITAL = "资本事件"


class Alias(NamedTuple):
    alias: str
    canonical: str
    category: str


# ── 别名表：越具体的昵称越要显式列出，否则会被更短的通用名吃掉 ──
ALIASES: list[Alias] = [
    # ⚠️ 反直觉别名必须写在前面（长度也足够长，双保险）
    Alias("北美大豆包", "Gemini", CAT_COMPETITOR),
    Alias("美国大豆包", "Gemini", CAT_COMPETITOR),
    Alias("美版大豆包", "Gemini", CAT_COMPETITOR),
    Alias("北美豆包", "Gemini", CAT_COMPETITOR),
    Alias("美国豆包", "Gemini", CAT_COMPETITOR),
    Alias("美版豆包", "Gemini", CAT_COMPETITOR),
    Alias("谷歌豆包", "Gemini", CAT_COMPETITOR),

    # 智谱系
    Alias("智谱清言", "智谱", CAT_ZHIPU),
    Alias("智谱AI", "智谱", CAT_ZHIPU),
    Alias("ChatGLM", "智谱", CAT_ZHIPU),
    Alias("GLM", "智谱", CAT_ZHIPU),
    Alias("智谱", "智谱", CAT_ZHIPU),
    Alias("唐杰", "唐杰", CAT_ZHIPU),
    Alias("张鹏", "智谱", CAT_ZHIPU),

    # 竞对
    Alias("月之暗面", "Kimi", CAT_COMPETITOR),
    Alias("Kimi", "Kimi", CAT_COMPETITOR),
    Alias("深度求索", "DeepSeek", CAT_COMPETITOR),
    Alias("DeepSeek", "DeepSeek", CAT_COMPETITOR),
    Alias("通义千问", "Qwen", CAT_COMPETITOR),
    Alias("通义", "Qwen", CAT_COMPETITOR),
    Alias("千问", "Qwen", CAT_COMPETITOR),
    Alias("Qwen", "Qwen", CAT_COMPETITOR),
    Alias("豆包", "豆包", CAT_COMPETITOR),
    Alias("文心一言", "文心", CAT_COMPETITOR),
    Alias("文心", "文心", CAT_COMPETITOR),
    Alias("混元", "混元", CAT_COMPETITOR),
    Alias("MiniMax", "MiniMax", CAT_COMPETITOR),
    Alias("零一万物", "零一万物", CAT_COMPETITOR),
    Alias("百川", "百川", CAT_COMPETITOR),
    Alias("阶跃", "阶跃", CAT_COMPETITOR),
    Alias("科大讯飞", "讯飞", CAT_COMPETITOR),
    Alias("商汤", "商汤", CAT_COMPETITOR),
    Alias("Anthropic", "Anthropic", CAT_COMPETITOR),
    Alias("Claude", "Anthropic", CAT_COMPETITOR),
    Alias("OpenAI", "OpenAI", CAT_COMPETITOR),
    Alias("ChatGPT", "OpenAI", CAT_COMPETITOR),
    Alias("Gemini", "Gemini", CAT_COMPETITOR),
    Alias("谷歌", "Google", CAT_COMPETITOR),
    Alias("Google", "Google", CAT_COMPETITOR),
    Alias("TPU", "Google", CAT_COMPETITOR),
    Alias("Llama", "Meta", CAT_COMPETITOR),
    Alias("Meta", "Meta", CAT_COMPETITOR),
    Alias("苹果", "苹果", CAT_COMPETITOR),
    Alias("荣耀", "荣耀", CAT_COMPETITOR),
    Alias("小米", "小米", CAT_COMPETITOR),
    Alias("华为", "华为", CAT_COMPETITOR),
    Alias("阿里", "阿里", CAT_COMPETITOR),
    Alias("腾讯", "腾讯", CAT_COMPETITOR),
    Alias("字节", "字节", CAT_COMPETITOR),
    Alias("百度", "百度", CAT_COMPETITOR),

    # 具身智能
    Alias("宇树科技", "宇树", CAT_EMBODIED),
    Alias("宇树", "宇树", CAT_EMBODIED),
    Alias("Unitree", "宇树", CAT_EMBODIED),
    Alias("智元机器人", "智元", CAT_EMBODIED),
    Alias("智元", "智元", CAT_EMBODIED),
    Alias("Agibot", "智元", CAT_EMBODIED),
    Alias("银河通用", "银河通用", CAT_EMBODIED),
    Alias("Galbot", "银河通用", CAT_EMBODIED),
    Alias("星动纪元", "星动纪元", CAT_EMBODIED),
    Alias("星海图", "星海图", CAT_EMBODIED),
    Alias("众擎", "众擎", CAT_EMBODIED),
    Alias("Figure", "Figure", CAT_EMBODIED),
    Alias("人形机器人", "人形机器人", CAT_EMBODIED),
    Alias("具身智能", "具身智能", CAT_EMBODIED),

    # 算力供应链
    Alias("英伟达", "英伟达", CAT_COMPUTE),
    Alias("NVIDIA", "英伟达", CAT_COMPUTE),
    Alias("黄仁勋", "英伟达", CAT_COMPUTE),
    Alias("AMD", "AMD", CAT_COMPUTE),
    Alias("台积电", "台积电", CAT_COMPUTE),
    Alias("中芯国际", "中芯", CAT_COMPUTE),
    Alias("中芯", "中芯", CAT_COMPUTE),
    Alias("华为昇腾", "昇腾", CAT_COMPUTE),
    Alias("昇腾", "昇腾", CAT_COMPUTE),
    Alias("寒武纪", "寒武纪", CAT_COMPUTE),
    Alias("摩尔线程", "摩尔线程", CAT_COMPUTE),
    Alias("Blackwell", "英伟达", CAT_COMPUTE),
    Alias("H100", "英伟达", CAT_COMPUTE),
    Alias("H200", "英伟达", CAT_COMPUTE),
    Alias("B200", "英伟达", CAT_COMPUTE),
    Alias("HBM", "HBM", CAT_COMPUTE),
    Alias("光模块", "光模块", CAT_COMPUTE),
    Alias("液冷", "液冷", CAT_COMPUTE),
    Alias("数据中心", "数据中心", CAT_COMPUTE),
    Alias("机柜", "机柜", CAT_COMPUTE),
    Alias("算力", "算力", CAT_COMPUTE),
    Alias("AI芯片", "芯片", CAT_COMPUTE),
    Alias("芯片", "芯片", CAT_COMPUTE),
    Alias("智算", "算力", CAT_COMPUTE),
    Alias("半导体", "芯片", CAT_COMPUTE),
    Alias("缺卡", "缺卡", CAT_COMPUTE),
    Alias("断供", "断供", CAT_COMPUTE),

    # 资本
    Alias("融资", "融资", CAT_CAPITAL),
    Alias("亿元融资", "融资", CAT_CAPITAL),
    Alias("轮融资", "融资", CAT_CAPITAL),
    Alias("上市", "上市", CAT_CAPITAL),
    Alias("IPO", "IPO", CAT_CAPITAL),
    Alias("亏损", "亏损", CAT_CAPITAL),
    Alias("盈利", "盈利", CAT_CAPITAL),
    Alias("估值", "估值", CAT_CAPITAL),
    Alias("亿美元", "估值", CAT_CAPITAL),
    Alias("收购", "收购", CAT_CAPITAL),
    Alias("破产", "破产", CAT_CAPITAL),
    Alias("裁员", "裁员", CAT_CAPITAL),
]

# 按别名长度倒序，保证「北美豆包」先于「豆包」被扫描到
_SORTED = sorted(ALIASES, key=lambda a: -len(a.alias))

# 命中后需要剪掉的、纯泛词（避免「算力」把一切算力相关都算成实体）
GENERIC = {"算力", "机柜", "液冷", "数据中心", "缺卡", "断供", "融资", "IPO",
           "估值", "收购", "破产", "裁员", "上市", "亏损", "盈利", "HBM", "光模块"}


# 规范实体 → 所属公司（竞对面板按公司聚合，避免 Gemini/Google、阿里/Qwen 重复计数）
COMPANY = {
    "Gemini": "Google", "Google": "Google", "TPU": "Google",
    "Qwen": "阿里巴巴", "阿里": "阿里巴巴", "通义": "阿里巴巴",
    "豆包": "字节跳动", "字节": "字节跳动",
    "Kimi": "月之暗面",
    "DeepSeek": "DeepSeek",
    "Anthropic": "Anthropic", "OpenAI": "OpenAI", "Meta": "Meta",
    "智谱": "智谱", "唐杰": "智谱", "张鹏": "智谱",
    "英伟达": "英伟达", "AMD": "AMD", "台积电": "台积电", "中芯": "中芯国际",
    "昇腾": "华为", "华为": "华为", "寒武纪": "寒武纪", "摩尔线程": "摩尔线程",
    "宇树": "宇树", "智元": "智元", "银河通用": "银河通用",
    "星动纪元": "星动纪元", "星海图": "星海图", "众擎": "众擎", "Figure": "Figure",
    "苹果": "苹果", "小米": "小米", "荣耀": "荣耀", "腾讯": "腾讯",
    "百度": "百度", "MiniMax": "MiniMax", "零一万物": "零一万物",
    "百川": "百川", "阶跃": "阶跃", "讯飞": "科大讯飞", "商汤": "商汤",
    "文心": "百度", "混元": "腾讯",
}

# 自己人：竞对面板不列（智谱/唐杰另有专题页）
SELF_COMPANIES = {"智谱"}

# 不是公司的产业链节点/事件词，不进竞对面板
NOT_COMPANY = {"芯片", "算力", "HBM", "光模块", "液冷", "数据中心", "机柜", "缺卡", "断供",
               "融资", "IPO", "估值", "收购", "破产", "裁员", "上市", "亏损", "盈利",
               "人形机器人", "具身智能"}


def company_of(entity: str) -> str | None:
    if entity in NOT_COMPANY:
        return None
    return COMPANY.get(entity, entity)


def title_category(title: str) -> str | None:
    """标题里命中智谱系 → 直接判为智谱板块（产品定位，不让位给顺带提到的竞对）"""
    if resolve(title)["categories"].get(CAT_ZHIPU):
        return CAT_ZHIPU
    return None


def resolve(text: str) -> dict:
    """返回 {entities:[规范实体], categories:{板块:[实体]}, spans:{别名:[(start,end,canonical)]}}

    最长匹配优先 + 跨度覆盖：被更长命中覆盖的短命中丢弃。
    """
    low = text.lower()
    taken: list[tuple[int, int, str, str]] = []   # (start, end, canonical, alias)

    for a in _SORTED:
        needle = a.alias.lower()
        start = 0
        while True:
            i = low.find(needle, start)
            if i < 0:
                break
            end = i + len(needle)
            # 与已接受的长命中重叠 → 丢弃（这就是「北美豆包」吃掉「豆包」的地方）
            if not any(not (end <= s or i >= e) for s, e, _, _ in taken):
                taken.append((i, end, a.canonical, a.alias))
            start = end

    encs = {c for _, _, c, _ in taken}
    cats: dict[str, set] = {}
    for _, _, canon, _ in taken:
        for a in ALIASES:
            if a.canonical == canon:
                cats.setdefault(a.category, set()).add(canon)
                break

    counts: dict[str, int] = {}
    for _, _, canon, _ in taken:
        counts[canon] = counts.get(canon, 0) + 1

    strong = [e for e in encs if e not in GENERIC]
    # 按出现次数降序（不按字符序，否则中文实体永远被拉丁字母挤到后面）
    strong.sort(key=lambda e: (-counts[e], e))
    return {
        "entities": strong,
        "all_entities": sorted(encs),
        "counts": counts,
        "categories": {k: sorted(v) for k, v in cats.items()},
    }
