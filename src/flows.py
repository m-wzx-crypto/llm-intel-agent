"""产业链流向抽取：从全文里抽 (from, to, channel, amount) 关系。

为什么单独一层：
- 旧实现只看「标题 + 120 字摘要」，而摘要经常是导语/导航/PDF 乱码，正文里的
  「完成 A 轮融资」「拟收购」「向 XX 增资」「中标 XX 项目」「采购算力」全被丢掉。
- 旧词表只认 10 个主体，OpenAI/Anthropic/台积电/中芯/甲骨文/润泽/英维克这些
  真实交易主体全被 `_plausible()` 拒掉。
- 旧句式只认「X 完成 N 亿元融资」，「获 Y 领投」「向 Y 增资」「由 Y 代工」
  「X 中标 Y 项目」「Y 向 X 采购算力」都不认。

本模块的取舍：
1. 先在全文中找**已知主体**（长别名优先，避免「北美豆包」→豆包），
   再看动词两侧最近的主体，方向按业务语义定（钱/资源从 from 流向 to）。
2. 已知主体不够时，用句式正则补**未知公司**，但必须有公司后缀或明确交易结构，
   宁可少抽，不把「三十载」「本轮」「投资者」当公司。
3. 每条边保留证据：报道数 n、金额（原文口径）、最近日期、最多 3 条原文。
4. 只输出 top N 条边，避免桑基图节点爆炸。

不编造：
- 抽不到就是抽不到；`from="资本市场"` 只用于「X 完成融资」但未披露投资方，
  这是真实的方向（钱来自资本市场），不是虚构主体。
"""
from __future__ import annotations

import re
from collections import defaultdict

# ── 产业链分层 + 主体别名 ──────────────────────────────────────────────
# 格式：(规范名, 层, 别名串)。别名用 | 分隔；拉丁别名按词边界匹配。
ENTITY_SPEC: list[tuple[str, str, str]] = [
    # 资本 / 投资方
    ("资本市场", "资本", "资本市场|一级市场|二级市场"),
    ("红杉资本", "资本", "红杉资本|红杉中国|红杉|Sequoia Capital|Sequoia"),
    ("高瓴资本", "资本", "高瓴资本|高瓴|Hillhouse"),
    ("软银", "资本", "软银|软银愿景基金|愿景基金|SoftBank|Vision Fund"),
    ("淡马锡", "资本", "淡马锡|Temasek"),
    ("GIC", "资本", "GIC|新加坡政府投资公司"),
    ("沙特公共投资基金", "资本", "沙特公共投资基金|沙特主权基金|PIF"),
    ("MGX", "资本", "MGX|阿布扎比MGX"),
    ("阿布扎比投资局", "资本", "阿布扎比投资局|ADIA"),
    ("穆巴达拉", "资本", "穆巴达拉|Mubadala"),
    ("贝莱德", "资本", "贝莱德|BlackRock"),
    ("黑石", "资本", "黑石|Blackstone"),
    ("凯雷", "资本", "凯雷|Carlyle"),
    ("KKR", "资本", "KKR"),
    ("华平投资", "资本", "华平投资|华平|Warburg Pincus"),
    ("春华资本", "资本", "春华资本|春华|Primavera"),
    ("高榕创投", "资本", "高榕创投|高榕"),
    ("IDG资本", "资本", "IDG资本|IDG"),
    ("经纬创投", "资本", "经纬创投|经纬中国|Matrix Partners|经纬"),
    ("源码资本", "资本", "源码资本|源码"),
    ("五源资本", "资本", "五源资本|五源"),
    ("启明创投", "资本", "启明创投|启明"),
    ("真格基金", "资本", "真格基金|真格"),
    ("创新工场", "资本", "创新工场|Sinovation"),
    ("奇绩创坛", "资本", "奇绩创坛|MiraclePlus"),
    ("联想创投", "资本", "联想创投"),
    ("腾讯投资", "资本", "腾讯投资"),
    ("阿里战投", "资本", "阿里战投|阿里巴巴战投"),
    ("百度风投", "资本", "百度风投|BV"),
    ("小米战投", "资本", "小米战投"),
    ("宁德时代", "资本", "宁德时代|CATL"),
    ("国家集成电路产业投资基金", "资本", "国家集成电路产业投资基金|大基金"),
    ("深创投", "资本", "深创投|深圳市创新投资集团"),
    ("中金资本", "资本", "中金资本"),
    ("招银国际", "资本", "招银国际"),
    ("中信证券", "资本", "中信证券|CITIC Securities"),
    ("中金公司", "资本", "中金公司|CICC"),
    ("摩根士丹利", "资本", "摩根士丹利|Morgan Stanley"),
    ("高盛", "资本", "高盛|Goldman Sachs"),
    ("摩根大通", "资本", "摩根大通|JPMorgan"),
    ("美国银行", "资本", "美国银行|Bank of America"),
    ("花旗", "资本", "花旗|Citi|Citigroup"),
    ("汇丰", "资本", "汇丰|HSBC"),
    ("瑞银", "资本", "瑞银|UBS"),
    ("巴克莱", "资本", "巴克莱|Barclays"),
    ("德意志银行", "资本", "德意志银行|Deutsche Bank"),
    ("a16z", "资本", "a16z|Andreessen Horowitz|Andreessen"),
    ("Founders Fund", "资本", "Founders Fund"),
    ("Thrive Capital", "资本", "Thrive Capital|Thrive"),
    ("老虎环球", "资本", "老虎环球|Tiger Global"),
    ("Coatue", "资本", "Coatue"),
    ("光速创投", "资本", "光速创投|光速|Lightspeed"),
    ("General Catalyst", "资本", "General Catalyst"),
    ("Index Ventures", "资本", "Index Ventures"),
    ("Benchmark", "资本", "Benchmark"),
    ("Accel", "资本", "Accel"),
    ("NEA", "资本", "NEA|New Enterprise Associates"),
    ("Khosla Ventures", "资本", "Khosla Ventures|Khosla"),
    ("GV", "资本", "GV|Google Ventures"),
    ("Salesforce Ventures", "资本", "Salesforce Ventures"),
    ("M12", "资本", "M12|Microsoft Ventures"),
    ("英特尔资本", "资本", "英特尔资本|Intel Capital"),
    ("高通创投", "资本", "高通创投|Qualcomm Ventures"),
    ("三星 Catalyst", "资本", "三星 Catalyst|Samsung Catalyst"),
    ("Aramco Ventures", "资本", "Aramco Ventures|沙特阿美"),
    ("卡塔尔投资局", "资本", "卡塔尔投资局|QIA"),
    ("科威特投资局", "资本", "科威特投资局|KIA"),
    ("加拿大养老金", "资本", "加拿大养老金|CPPIB"),
    ("挪威主权基金", "资本", "挪威主权基金|NBIM"),
    ("三菱", "资本", "三菱|Mitsubishi"),
    ("三井", "资本", "三井|Mitsui"),
    ("住友", "资本", "住友|Sumitomo"),
    ("李泽湘", "资本", "李泽湘|X-Tech"),

    # 芯片 / 代工 / 服务器
    ("英伟达", "芯片", "英伟达|NVIDIA|Nvidia|辉达"),
    ("AMD", "芯片", "AMD|超威半导体"),
    ("英特尔", "芯片", "英特尔|Intel"),
    ("博通", "芯片", "博通|Broadcom"),
    ("高通", "芯片", "高通|Qualcomm"),
    ("Marvell", "芯片", "Marvell|美满电子|美满"),
    ("Arm", "芯片", "Arm|安谋"),
    ("台积电", "芯片", "台积电|TSMC|台积"),
    ("三星电子", "芯片", "三星电子|三星|Samsung"),
    ("SK海力士", "芯片", "SK海力士|海力士|SK Hynix|Hynix"),
    ("美光", "芯片", "美光|Micron"),
    ("中芯国际", "芯片", "中芯国际|中芯|SMIC"),
    ("华虹半导体", "芯片", "华虹半导体|华虹宏力|华虹"),
    ("长江存储", "芯片", "长江存储|YMTC"),
    ("长鑫存储", "芯片", "长鑫存储|长鑫|CXMT"),
    ("寒武纪", "芯片", "寒武纪|Cambricon"),
    ("海光信息", "芯片", "海光信息|海光|Hygon"),
    ("摩尔线程", "芯片", "摩尔线程|Moore Threads"),
    ("壁仞科技", "芯片", "壁仞科技|壁仞|Biren"),
    ("燧原科技", "芯片", "燧原科技|燧原|Enflame"),
    ("沐曦集成", "芯片", "沐曦集成|沐曦|MetaX"),
    ("昆仑芯", "芯片", "昆仑芯|百度昆仑芯"),
    ("平头哥", "芯片", "平头哥|T-Head"),
    ("昇腾", "芯片", "昇腾|Ascend"),
    ("华为", "芯片", "华为|Huawei"),
    ("阿斯麦", "芯片", "阿斯麦|ASML"),
    ("应用材料", "芯片", "应用材料|Applied Materials"),
    ("泛林", "芯片", "泛林|Lam Research"),
    ("科磊", "芯片", "科磊|KLA"),
    ("东京电子", "芯片", "东京电子|Tokyo Electron"),
    ("新思科技", "芯片", "新思科技|新思|Synopsys"),
    ("楷登电子", "芯片", "楷登电子|楷登|Cadence"),
    ("华大九天", "芯片", "华大九天|Empyrean"),
    ("芯原", "芯片", "芯原|VeriSilicon"),
    ("澜起科技", "芯片", "澜起科技|澜起|Montage"),
    ("兆易创新", "芯片", "兆易创新|兆易|GigaDevice"),
    ("韦尔股份", "芯片", "韦尔股份|韦尔|Will Semiconductor"),
    ("思特威", "芯片", "思特威|SmartSens"),
    ("武汉楚兴", "芯片", "武汉楚兴|楚兴"),
    ("飞凌微", "芯片", "飞凌微"),
    ("粤芯半导体", "芯片", "粤芯半导体|粤芯"),
    ("源杰科技", "芯片", "源杰科技|源杰"),
    ("利扬芯片", "芯片", "利扬芯片|利扬"),
    ("佰维存储", "芯片", "佰维存储|佰维"),
    ("普冉股份", "芯片", "普冉股份|普冉"),
    ("东芯股份", "芯片", "东芯股份|东芯"),
    ("捷捷微电", "芯片", "捷捷微电|捷捷"),
    ("众望达", "芯片", "众望达|众望达光电"),
    ("弘信电子", "算力", "弘信电子|弘信"),
    ("Sand.ai", "模型", "Sand.ai|Sandai|Sand AI"),
    ("中际旭创", "芯片", "中际旭创|Innolight"),
    ("新易盛", "芯片", "新易盛|Eoptolink"),
    ("光迅科技", "芯片", "光迅科技|光迅|Accelink"),
    ("天孚通信", "芯片", "天孚通信|天孚|T&S"),
    ("工业富联", "芯片", "工业富联|富士康工业互联网|FII"),
    ("富士康", "芯片", "富士康|鸿海|Foxconn"),
    ("广达", "芯片", "广达|Quanta"),
    ("纬创", "芯片", "纬创|Wistron"),
    ("英业达", "芯片", "英业达|Inventec"),
    ("和硕", "芯片", "和硕|Pegatron"),
    ("仁宝", "芯片", "仁宝|Compal"),
    ("捷普", "芯片", "捷普|Jabil"),
    ("伟创力", "芯片", "伟创力|Flex"),
    ("立讯精密", "芯片", "立讯精密|立讯|Luxshare"),
    ("浪潮信息", "芯片", "浪潮信息|浪潮|Inspur"),
    ("中科曙光", "芯片", "中科曙光|曙光|Sugon"),
    ("超微", "芯片", "超微|Supermicro|Super Micro"),
    ("戴尔", "芯片", "戴尔|Dell"),
    ("联想", "芯片", "联想|Lenovo"),
    ("新华三", "芯片", "新华三|H3C"),
    ("中兴通讯", "芯片", "中兴通讯|中兴|ZTE"),
    ("超聚变", "芯片", "超聚变|xFusion"),
    ("宁畅", "芯片", "宁畅|Nettrix"),
    ("神州数码", "芯片", "神州数码|Digital China"),
    ("紫光股份", "芯片", "紫光股份|紫光|Unisplendour"),
    ("紫光展锐", "芯片", "紫光展锐|展锐|UNISOC"),

    # 模型 / 应用
    ("OpenAI", "模型", "OpenAI|ChatGPT"),
    ("Anthropic", "模型", "Anthropic|Claude"),
    ("xAI", "模型", "xAI|X AI"),
    ("Google DeepMind", "模型", "Google DeepMind|DeepMind|谷歌DeepMind"),
    ("Google", "模型", "Google|谷歌"),
    ("Meta", "模型", "Meta|Facebook|Llama"),
    ("Mistral AI", "模型", "Mistral AI|Mistral"),
    ("Cohere", "模型", "Cohere"),
    ("Perplexity", "模型", "Perplexity"),
    ("Safe Superintelligence", "模型", "Safe Superintelligence|SSI"),
    ("Thinking Machines", "模型", "Thinking Machines Lab|Thinking Machines"),
    ("智谱", "模型", "智谱AI|智谱|Zhipu|ChatGLM|GLM"),
    ("月之暗面", "模型", "月之暗面|Moonshot AI|Moonshot|Kimi"),
    ("MiniMax", "模型", "MiniMax|稀宇科技"),
    ("百川智能", "模型", "百川智能|百川"),
    ("零一万物", "模型", "零一万物|01.AI|01AI"),
    ("阶跃星辰", "模型", "阶跃星辰|阶跃|StepFun"),
    ("DeepSeek", "模型", "DeepSeek|深度求索"),
    ("阿里巴巴", "模型", "阿里巴巴|阿里|Alibaba"),
    ("字节跳动", "模型", "字节跳动|字节|ByteDance|豆包|Doubao"),
    ("腾讯", "模型", "腾讯|Tencent|混元|Hunyuan"),
    ("百度", "模型", "百度|Baidu|文心|ERNIE"),
    ("科大讯飞", "模型", "科大讯飞|讯飞|iFlytek"),
    ("商汤", "模型", "商汤|SenseTime"),
    ("面壁智能", "模型", "面壁智能|面壁|ModelBest"),
    ("出门问问", "模型", "出门问问|Mobvoi"),
    ("澜舟科技", "模型", "澜舟科技|澜舟|Langboat"),
    ("生数科技", "模型", "生数科技|生数|ShengShu"),
    ("爱诗科技", "模型", "爱诗科技|爱诗|PixVerse"),
    ("潞晨科技", "模型", "潞晨科技|潞晨|Colossal-AI"),
    ("无问芯穹", "模型", "无问芯穹|Infinigence"),
    ("硅基流动", "模型", "硅基流动|SiliconFlow"),
    ("上海人工智能实验室", "模型", "上海人工智能实验室|Shanghai AI Lab"),
    ("北京智源", "模型", "北京智源人工智能研究院|北京智源|智源|BAAI"),
    ("中科院", "模型", "中国科学院|中科院|CAS"),
    ("清华大学", "模型", "清华大学|清华|Tsinghua"),
    ("北京大学", "模型", "北京大学|北大|Peking University"),
    ("斯坦福大学", "模型", "斯坦福大学|斯坦福|Stanford"),
    ("麻省理工", "模型", "麻省理工学院|麻省理工|MIT"),
    ("苹果", "模型", "苹果|Apple"),
    ("特斯拉", "模型", "特斯拉|Tesla"),
    ("小米", "模型", "小米|Xiaomi"),
    ("荣耀", "模型", "荣耀|Honor"),
    ("OPPO", "模型", "OPPO"),
    ("vivo", "模型", "vivo"),
    ("比亚迪", "模型", "比亚迪|BYD"),
    ("微软", "模型", "微软|Microsoft"),
    ("Salesforce", "模型", "Salesforce"),
    ("SAP", "模型", "SAP"),
    ("IBM", "模型", "IBM"),

    # 算力 / 云 / 数据中心
    ("阿里云", "算力", "阿里云|阿里云计算|Alibaba Cloud"),
    ("腾讯云", "算力", "腾讯云|Tencent Cloud"),
    ("华为云", "算力", "华为云|Huawei Cloud"),
    ("火山引擎", "算力", "火山引擎|Volcano Engine"),
    ("百度智能云", "算力", "百度智能云|Baidu AI Cloud"),
    ("金山云", "算力", "金山云|Kingsoft Cloud"),
    ("优刻得", "算力", "优刻得|UCloud"),
    ("青云科技", "算力", "青云科技|青云|QingCloud"),
    ("天翼云", "算力", "天翼云"),
    ("移动云", "算力", "移动云"),
    ("联通云", "算力", "联通云"),
    ("微软云", "算力", "微软云|Azure|Microsoft Azure"),
    ("AWS", "算力", "AWS|Amazon Web Services|亚马逊云"),
    ("谷歌云", "算力", "谷歌云|Google Cloud"),
    ("甲骨文云", "算力", "甲骨文云|Oracle Cloud"),
    ("亚马逊", "算力", "亚马逊|Amazon"),
    ("甲骨文", "算力", "甲骨文|Oracle"),
    ("CoreWeave", "算力", "CoreWeave"),
    ("Lambda", "算力", "Lambda Labs|Lambda"),
    ("Crusoe", "算力", "Crusoe Energy|Crusoe"),
    ("Together AI", "算力", "Together AI|Together"),
    ("Fireworks AI", "算力", "Fireworks AI|Fireworks"),
    ("Groq", "算力", "Groq"),
    ("Cerebras", "算力", "Cerebras"),
    ("SambaNova", "算力", "SambaNova"),
    ("Graphcore", "算力", "Graphcore"),
    ("中国移动", "算力", "中国移动|China Mobile"),
    ("中国电信", "算力", "中国电信|China Telecom"),
    ("中国联通", "算力", "中国联通|China Unicom"),
    ("国家电网", "算力", "国家电网|State Grid"),
    ("南方电网", "算力", "南方电网"),
    ("国家超算中心", "算力", "国家超算中心|超算中心"),
    ("世纪互联", "数据中心", "世纪互联|VNET"),
    ("万国数据", "数据中心", "万国数据|GDS"),
    ("润泽科技", "数据中心", "润泽科技|润泽"),
    ("秦淮数据", "数据中心", "秦淮数据|Chindata"),
    ("奥飞数据", "数据中心", "奥飞数据|奥飞"),
    ("数据港", "数据中心", "数据港|Data Port"),
    ("光环新网", "数据中心", "光环新网|Sinnet"),
    ("宝信软件", "数据中心", "宝信软件|宝信|Baosight"),
    ("科华数据", "数据中心", "科华数据|科华"),
    ("英维克", "数据中心", "英维克|Envicool"),
    ("申菱环境", "数据中心", "申菱环境|申菱"),
    ("高澜股份", "数据中心", "高澜股份|高澜"),
    ("依米康", "数据中心", "依米康|Yimikang"),
    ("佳力图", "数据中心", "佳力图|Canatal"),
    ("网宿科技", "数据中心", "网宿科技|网宿|Wangsu"),
    ("鹏博士", "数据中心", "鹏博士|Dr.Peng"),
    ("Equinix", "数据中心", "Equinix"),
    ("Digital Realty", "数据中心", "Digital Realty"),
    ("Iron Mountain", "数据中心", "Iron Mountain"),
    ("QTS", "数据中心", "QTS"),
    ("Vantage", "数据中心", "Vantage"),
    ("CyrusOne", "数据中心", "CyrusOne"),
]

# 通用节点：只在「自建数据中心」这类结构里当 to，不参与收购/投资主体匹配
GENERIC_NODES = {"数据中心", "智算中心", "算力中心", "超算中心", "AI工厂", "AI 工厂",
                 "芯片", "算力", "模型", "基金", "基金/芯片"}

# 已知主体 → (规范名, 层)
_KNOWN: dict[str, tuple[str, str]] = {}
for canon, layer, aliases in ENTITY_SPEC:
    _KNOWN[canon.lower()] = (canon, layer)
    for a in aliases.split("|"):
        a = a.strip()
        if a:
            _KNOWN[a.lower()] = (canon, layer)

# 长别名优先；拉丁别名按词边界
_ALIASES = sorted({a.lower(): (canon, layer) for canon, layer, aliases in ENTITY_SPEC
                   for a in aliases.split("|") if a.strip()}.items(),
                  key=lambda kv: -len(kv[0]))


def _is_ascii(s: str) -> bool:
    return all(ord(ch) < 128 for ch in s)


def find_entities(text: str) -> list[tuple[int, int, str, str]]:
    """返回 [(start, end, 规范名, 层)]；长匹配优先，被覆盖的短匹配丢弃。"""
    low = (text or "").lower()
    taken: list[tuple[int, int, str, str]] = []
    for alias, (canon, layer) in _ALIASES:
        if not alias:
            continue
        if _is_ascii(alias):
            pat = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        else:
            pat = re.escape(alias)
        start = 0
        while True:
            m = re.search(pat, low[start:])
            if not m:
                break
            i, j = start + m.start(), start + m.end()
            if not any(not (j <= s or i >= e) for s, e, _, _ in taken):
                taken.append((i, j, canon, layer))
            start = j
    return sorted(taken)


# ── 主体/金额清洗 ──────────────────────────────────────────────────────
_PREFIX = ("据悉", "据报道", "消息称", "公告称", "宣布", "正式", "拟", "将", "已", "计划",
           "准备", "今日", "近日", "日前", "本轮", "本次", "该笔", "其中", "同时", "此外",
           "目前", "现在", "最新", "公司", "其", "该")
_SUFFIX = ("宣布", "表示", "称", "方面", "完成", "获得", "获", "正式", "拟", "将", "已",
           "计划", "与", "和", "向", "对", "的", "等", "牵头", "联合", "共同", "投资方")

_BAD_CONTAINS = ("本文", "记者", "编辑", "来源", "公告", "研报", "日报", "快讯", "消息",
                 "新闻", "作者", "免责", "版权", "转载", "公众号", "同比", "环比",
                 "上半年", "下半年", "前三季度", "亿元", "万美元", "亿美元", "千万",
                 "百万", "轮融资", "估值", "市值", "股价", "涨停", "跌停", "产业链",
                 "赛道", "行业", "市场", "客户", "供应商", "股东", "投资者", "出资方",
                 "交易", "标的", "本轮", "该笔", "本次", "计划", "预计", "据悉", "报道",
                 "拟", "收购方", "被收购", "项目", "亿元", "美元", "港元", "人民币",
                 "不构成", "建议", "尚未", "已经", "可能", "表示", "认为", "指出",
                 "显示", "宣布", "透露", "据悉", "根据", "目前", "以及", "此外",
                 "同时", "其中", "方面", "产业", "股权", "贷款", "信用评级", "规模",
                 "私人", "级别", "管理合伙", "合伙企业", "投资基金", "有限公司的",
                 "亿元级", "亿美元级", "的股权", "和贷款", "与贷款", "的", "史上",
                 "最大", "首个", "首单", "首款", "首家", "创纪录", "历史性", "迎来",
                 "完成创纪录", "史最大", "募集资金", "发行", "股票", "对象",
                 "控股主体", "最上层", "集团公司", "重组完成", "本次", "拟使用",
                 "净买入", "净卖出", "净流入", "净流出", "南向资金", "北向资金",
                 "买入", "卖出", "增持", "减持", "港股跌", "港股涨", "涨超", "跌超",
                 "收涨", "收跌", "运作", "依照")
_BAD_EXACT = {"公司", "集团", "企业", "机构", "平台", "生态", "项目", "市场", "行业",
              "资本", "基金", "银行", "政府", "国家", "中国", "美国", "欧洲", "日本",
              "韩国", "全球", "AI", "人工智能", "大模型", "芯片", "算力", "数据中心",
              "智算中心", "超算中心", "服务器", "GPU", "半导体", "机器人", "具身智能",
              "自动驾驶", "上市公司", "科创公司", "初创公司", "独角兽", "某公司",
              "该公司", "一家公司", "这家公司", "数千万", "数百万", "数十亿", "一百亿",
              "宣布", "建议", "方面", "产业", "尚未", "不构成", "议私人", "级信用评级",
              "管理合伙企业", "投资", "融资", "收购", "增资", "注资", "采购", "订单",
              "投资方", "收购方", "被收购方", "资本市场", "项目方", "客户", "供应商",
              "全资子公司", "控股子公司", "子公司", "参股公司", "下属子公司",
              "目标公司", "标的公司", "发行人", "保荐机构", "公募基金", "私募基金",
              "基金公司", "基金管理人"}

_GENERIC_CN_STEM = {"电子科技", "信息科技", "网络科技", "智能科技", "光电科技",
                    "半导体科技", "通信科技", "通讯科技", "数字科技", "数据科技",
                    "科技", "电子", "信息", "网络", "智能", "光电", "半导体",
                    "通信", "通讯", "数字", "数据", "软件", "材料", "新能源"}

_CN_SUFFIX = ("科技", "智能", "集团", "资本", "基金", "半导体", "微电子", "光电", "电子",
              "通信", "网络", "数据", "云计算", "计算", "机器人", "汽车", "能源", "电力",
              "控股", "股份", "有限公司", "研究院", "实验室", "大学", "银行", "证券",
              "保险", "资管", "创投", "投资", "科技公司", "控股集团")
_EN_SUFFIX = ("Inc", "Inc.", "Corp", "Corp.", "Corporation", "Ltd", "Ltd.", "LLC",
              "Co", "Co.", "Technologies", "Technology", "Systems", "Labs", "Lab",
              "Group", "Holdings", "Capital", "Partners", "Fund", "Ventures",
              "Networks", "Solutions", "AI", "Cloud", "Energy", "Data",
              "Semiconductor", "Semiconductors", "Motors", "Media", "Interactive",
              "Studios", "Software", "Robotics", "University", "College",
              "Resources", "Industries", "Materials", "Power", "Digital",
              "Communications", "Services", "Health", "Pharma", "Bio",
              "Manufacturing", "Center", "Infrastructure")


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]{0,200}>", " ", s or "")
    s = s.replace("“", "").replace("”", "").replace("\"", "")
    s = re.sub(r"[\s\u3000]+", " ", s).strip(" \t\n\r，。；：:、（）()【】[]《》<>！？!?·-—/\\|")
    for _ in range(3):
        for p in _PREFIX:
            if s.startswith(p) and len(s) > len(p) + 1:
                s = s[len(p):].lstrip(" 　的")
        for p in _SUFFIX:
            if s.endswith(p) and len(s) > len(p) + 1:
                s = s[:-len(p)].rstrip(" 　的")
    # 去掉括号里的补充说明（保留括号里的外文名的情况交给别名表）
    s = re.sub(r"[（(][^）)]{0,30}[）)]", "", s).strip()
    return s


_LEAD_MOD = ("据悉", "据报道", "消息称", "公告称", "宣布", "正式", "日前", "近日", "今日",
             "此前", "随后", "目前", "现在", "最新", "其中", "同时", "此外", "目前",
             "年", "月", "日", "现", "据", "称", "表示", "认为", "指出", "显示", "透露",
             "报道", "消息", "我们", "拟", "将", "已", "计划", "预计", "或", "并", "也",
             "还", "但", "而", "且", "因此", "全球", "中国", "美国", "欧洲", "日本",
             "韩国", "印度", "中东", "该", "其", "这家", "一家", "某")
_TAIL_MOD = ("宣布", "表示", "称", "试图", "计划", "拟", "将", "已", "方面", "全球",
             "中国", "智库", "官方", "官网", "最新", "此次", "本轮", "该笔", "交易",
             "投资", "收购", "增资", "融资", "牵头", "联合", "共同", "领投", "参投")
_LEAD_MOD = tuple(sorted(_LEAD_MOD, key=len, reverse=True))
_TAIL_MOD = tuple(sorted(_TAIL_MOD, key=len, reverse=True))

_EN_GENERIC = {"AI", "AGI", "GPU", "CPU", "TPU", "HBM", "SSD", "LCD", "LED", "OLED",
               "IPO", "CEO", "CFO", "CTO", "COO", "API", "SaaS", "LLM", "USD", "SEC",
               "FDA", "EU", "US", "UK", "China", "America", "Europe", "Asia", "Mini",
               "Pro", "Max", "Plus", "Ultra", "Cloud", "Data", "Compute", "Model",
               "Agent", "Token", "Series", "Round", "Deal", "Report"}


_EN_CORP_SUFFIX = ("Corporation", "Holdings", "Technologies", "Technology", "Systems",
                   "Group", "Inc.", "Inc", "Corp.", "Corp", "Ltd.", "Ltd", "LLC",
                   "Limited", "Co.", "Co", "plc", "PLC", "N.V.", "NV", "AG")


def _lookup_known(s: str) -> tuple[str, str] | None:
    """精确命中别名；允许剥掉「年/据悉/试图/智库」这类修饰后命中。"""
    hit = _KNOWN.get(s.lower())
    if hit:
        return hit
    t = s
    # NVIDIA CORP / Arm Holdings plc → 去掉公司后缀再查一次
    for p in _EN_CORP_SUFFIX:
        if t.lower().endswith(" " + p.lower()) and len(t) > len(p) + 2:
            hit = _KNOWN.get(t[:-len(p)].strip().lower())
            if hit:
                return hit
            break
    for _ in range(3):
        changed = False
        for p in _LEAD_MOD:
            if t.startswith(p) and len(t) > len(p) + 1:
                t = t[len(p):].lstrip(" 　的")
                changed = True
        for p in _TAIL_MOD:
            if t.endswith(p) and len(t) > len(p) + 1:
                t = t[:-len(p)].rstrip(" 　的")
                changed = True
        if not changed:
            break
        hit = _KNOWN.get(t.lower())
        if hit:
            return hit
    return None


def _plausible(name: str, trusted: bool = False, allow_bare: bool = False) -> bool:
    if not name:
        return False
    n = name.strip()
    if len(n) < 2:
        return False
    if not _is_ascii(n) and len(n) > 24:
        return False
    if _is_ascii(n) and len(n) > 48:   # 「Quanome Technologies, Inc.」这类全称更长
        return False
    if n in _BAD_EXACT:
        return False
    if any(b in n for b in _BAD_CONTAINS):
        return False
    if re.fullmatch(r"[\d\s.,%+\-—/]+", n):
        return False
    if "的" in n:
        return False
    if n[0] in "的了和与及或在为是有无未将已拟获被向对从到把让使给替议商称说表认指显曝传据信不尚可应需要该此其这那各共同再又也都只仅还并而且或以及由于一二三四五六七八九十我你他她它后前首次第本该此":
        return False
    if n[-1] in "的了等和与及或在为是有无未将已拟获被向对从到把让使给替议商称说表认指显曝传据息划计预期模级面方业产权款价额量率比数些种类者们中内外上下前后布成席管回":
        return False
    if _is_ascii(n):
        if n in _EN_GENERIC or n.lower() in {x.lower() for x in _EN_GENERIC}:
            return False
        low = n.lower()
        if any(low.endswith(sfx.lower()) for sfx in _EN_SUFFIX):
            return True
        if allow_bare:
            return bool(re.fullmatch(r"[A-Z][A-Za-z&.\-]{2,28}", n)) and bool(re.search(r"[aeiouAEIOU]", n)) \
                and not n.isupper()
        return False
    # 「电子科技股份有限公司」这类是长公司名被截断后的残片，不是主体
    for sfx in ("股份有限公司", "有限责任公司", "有限公司"):
        if n.endswith(sfx) and n[:-len(sfx)] in _GENERIC_CN_STEM:
            return False
    # 中文
    if trusted:
        return True
    for sfx in _CN_SUFFIX:
        if n.endswith(sfx):
            stem = n[:-len(sfx)]
            if len(stem) < 2 or stem in {"区域", "股份", "控股", "集团", "有限", "中国", "全球"}:
                return False
            return len(n) >= 4
    # 未知中文标的：只接受 3–6 字专名（「武汉楚兴」「伽蓝特」），且必须出现在宾语位置
    return allow_bare and 3 <= len(n) <= 6


def canon(name: str, trusted: bool = False, allow_bare: bool = False) -> tuple[str, str] | None:
    """清洗 + 归一。返回 (规范名, 层)；不可信返回 None。"""
    s = _clean(name)
    if not s:
        return None
    hit = _lookup_known(s)
    if hit:
        return hit
    if not _plausible(s, trusted, allow_bare):
        return None
    return s, "其他"


# 金额：中文/英文/符号，保留原文口径，不做汇率换算
_AMT_CN = re.compile(
    r"(?:约|近|超|逾|高达|不低于|至少)?\s*"
    r"(?:\d[\d,]*(?:\.\d+)?|[一二三四五六七八九十百千万亿两]+|数|数十|数百)"
    r"\s*(?:亿|千万|百万|万)?\s*(?:美元|美金|元人民币|人民币|元|港元|港币|欧元|日元)?"
)
_AMT_EN = re.compile(
    r"(?:\$|US\$|USD|€|EUR|£|GBP)\s?\d[\d.,]*\s*(?:million|billion|M|B|mn|bn)?"
    r"|\d[\d.,]*\s*(?:million|billion)\s*(?:dollars|USD)?",
    re.I,
)


def _amount_near(text: str, start: int, end: int, pad: int = 40) -> str:
    return _amount(text[max(0, start - 24): min(len(text), end + pad)])


def _amount(text: str) -> str:
    best = ""
    for m in list(_AMT_CN.finditer(text or "")) + list(_AMT_EN.finditer(text or "")):
        s = re.sub(r"[\s,]+", "", m.group(0))
        if not s:
            continue
        # 过滤纯年份/百分比/小数字
        if re.fullmatch(r"(?:19|20)\d{2}", s):
            continue
        if "%" in s:
            continue
        if not (re.search(r"[亿万]", s) or re.search(r"[美元€£$]|million|billion|M|B", s, re.I)):
            continue
        if s in ("万", "亿", "千万", "百万") or not re.search(r"\d|[一二三四五六七八九十百千万亿两数]", s):
            continue
        if len(s) > len(best):
            best = s
    # 「数千万」「数十亿」这类没被上面的数字分支吃到的
    if not best:
        m = re.search(r"(?:数|数十|数百)[亿万](?:美元|元|人民币)?", text or "")
        if m:
            best = m.group(0)
    return best[:24]


# ── 主体捕获：先已知实体，后句式兜底 ───────────────────────────────────
_ACT = (r"(?:[\u4e00-\u9fa5]{2,12}(?:·[\u4e00-\u9fa5]{1,8})?"
        r"|[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3})")


def _nearest(ents, pos: int, side: str, max_gap: int = 36):
    if side == "before":
        cand = [e for e in ents if e[1] <= pos and pos - e[1] <= max_gap]
        return max(cand, key=lambda e: e[1]) if cand else None
    cand = [e for e in ents if e[0] >= pos and e[0] - pos <= max_gap]
    return min(cand, key=lambda e: e[0]) if cand else None


def _all_before(ents, pos: int, max_gap: int = 30):
    return [e for e in ents if e[1] <= pos and pos - e[1] <= max_gap]


def _all_after(ents, pos: int, max_gap: int = 30):
    return [e for e in ents if e[0] >= pos and e[0] - pos <= max_gap]


def _mk(frm, to, channel, amount="", quote="", trusted=False,
        allow_bare_from=False, allow_bare_to=True):
    a = canon(frm, trusted, allow_bare=allow_bare_from)
    b = canon(to, trusted, allow_bare=allow_bare_to)
    if not a or not b:
        return None
    if a[0] == b[0]:
        return None
    if a[0] in GENERIC_NODES or b[0] in GENERIC_NODES:
        return None
    return {"from": a[0], "to": b[0], "channel": channel,
            "amount": amount, "quote": quote[:180],
            "from_layer": a[1], "to_layer": b[1]}


# 逐条句式规则（兜底未知公司；方向以「钱/资源流向」为准）
_RULES = [
    # 融资：X 完成/获得 ... 融资 → 资本市场 → X
    ("融资", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:完成|获得|获|拿下|宣布完成)\s*"
        r"(?:新一轮|本轮|首轮|天使轮|Pre-?[A-Za-z]轮|[A-Za-z]轮|战略)?\s*"
        r"(?P<amt>(?:约|近|超|逾)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)\s*"
        r"(?:融资|募资)")),
    # 融资：X 第二轮千亿融资落地 → 资本市场 → X
    ("融资", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:第[一二三四五六七八九十\d]+轮|新一轮|本轮)?\s*"
        r"(?P<amt>(?:千亿|百亿|数十亿|数亿|数千万|\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?))\s*"
        r"(?:融资|募资)\s*(?:落地|完成|到账|交割|敲定|收官)")),
    # 融资：获得 Y 投资/领投 → Y → X
    ("投资", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:获|获得)\s*(?P<b>" + _ACT + r")\s*"
        r"(?:领投|参投|战略投资|投资|注资)\s*(?P<amt>(?:约|近|超|逾)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)?")),
    # 领投/参投：X 领投 Y
    ("投资", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:领投|参投|战略投资|投资|入股|增持)\s*"
        r"(?P<b>" + _ACT + r")")),
    # 增资：X 向/对 Y 增资 → X → Y
    ("出资", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:拟)?(?:向|对)\s*(?P<b>" + _ACT + r")\s*"
        r"(?:增资|注资|投资)\s*(?P<amt>(?:约|近|超|逾|不低于)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)?")),
    # 收购：X 收购/控股 Y → X → Y
    ("收购", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:拟|宣布|正式|已|将)?\s*"
        r"(?:收购|并购|全资收购|要约收购|买下|控股|取得.{0,4}控制权)\s*"
        r"(?P<b>" + _ACT + r")")),
    # 被收购：Y 被 X 收购 → X → Y
    ("收购", re.compile(
        r"(?P<b>" + _ACT + r")\s*(?:被|遭)\s*(?P<a>" + _ACT + r")\s*(?:收购|并购)")),
    # 采购：X 向/从 Y 采购/租用 → X → Y
    ("采购", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:向|从)\s*(?P<b>" + _ACT + r")\s*"
        r"(?:采购|订购|购买|租用|租赁)\s*(?:算力|GPU|芯片|服务器|机柜|云服务|光模块|液冷)?\s*"
        r"(?P<amt>(?:约|近|超|逾)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)?")),
    # 中标：X 中标 Y 项目 → Y → X
    ("订单", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:中标|拿下)\s*(?P<b>" + _ACT + r")?\s*"
        r"(?:的)?(?:项目|订单|合同|标段|大单)")),
    # 合同：X 与 Y 签署 N 亿元合同 → X → Y
    ("合同", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:与|和)\s*(?P<b>" + _ACT + r")\s*"
        r"(?:签署|签订|达成)\s*(?P<amt>(?:约|近|超|逾)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)?\s*"
        r"(?:合同|协议|订单|合作)")),
    # 订单：Y 获 X 订单 → X → Y
    ("订单", re.compile(
        r"(?P<b>" + _ACT + r")\s*(?:获|获得)\s*(?P<a>" + _ACT + r")\s*"
        r"(?:的)?(?:订单|采购订单|合同|大单)")),
    # 代工：X 委托/交由 Y 代工 → X → Y
    ("代工", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:委托|交由)\s*(?P<b>" + _ACT + r")\s*(?:代工|生产|制造)")),
    # 代工：Y 为 X 代工 → X → Y
    ("代工", re.compile(
        r"(?P<b>" + _ACT + r")\s*(?:为|替)\s*(?P<a>" + _ACT + r")\s*(?:代工|生产|制造|供货)")),
    # 自建：X 投资 N 亿建设数据中心 → X → 自建数据中心
    ("自建", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:投资|投入|斥资|计划投资)\s*"
        r"(?P<amt>(?:约|近|超|逾)?\s*\d+(?:\.\d+)?\s*(?:亿|千万|百万|万)?\s*(?:美元|元|人民币)?)?\s*"
        r"(?:建设|建造|自建|扩建|投建)\s*(?P<what>数据中心|智算中心|算力中心|超算中心|AI工厂|AI 工厂)")),
    # IPO：X 启动/敲钟/万亿上市 → 资本市场 → X
    ("IPO", re.compile(
        r"(?P<a>" + _ACT + r")\s*(?:敲钟|启动|完成|正式|即将|拟)?\s*"
        r"(?:万亿|千亿|百亿|亿)?\s*(?:IPO|上市|挂牌)")),
]

# 英文句式（TechCrunch / Crunchbase / SEC）
_EN_ACT = (r"(?:[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,4})")
# 目标可以是资产名（18MW Computing Center），允许数字开头
_EN_TARGET = (r"(?:[A-Z0-9][A-Za-z0-9&.\-]*(?:\s+[A-Z0-9][A-Za-z0-9&.\-]*){0,4})")
_EN_RULES = [
    ("融资", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:raises?|raised|secures?|secured|closes?|closed|lands?|landed)\s+"
        r"(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B|mn|bn)?)\s*(?:dollars|USD)?\s*"
        r"(?:in\s+)?(?:a\s+)?(?:new\s+)?(?:funding|round|financing|investment)"
        r"(?:\s+led\s+by\s+(?P<b>" + _EN_TARGET + r"))?", re.I)),
    ("收购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:acquires?|acquired|to\s+acquire|buys?|bought|to\s+buy)\s+"
        r"(?P<b>" + _EN_TARGET + r")(?:\s+for\s+(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?))?", re.I)),
    ("投资", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:invests?|invested|backs?|backed|leads?|led)\s+"
        r"(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?)?\s*(?:in|into)?\s*"
        r"(?P<b>" + _EN_TARGET + r")", re.I)),
    ("合同", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:signs?|signed|wins?|won)\s+"
        r"(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?)?\s*"
        r"(?:deal|contract|agreement|order)\s+(?:with|from)\s+(?P<b>" + _EN_TARGET + r")", re.I)),
    ("采购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:buys?|bought|purchases?|purchased|leases?|leased)\s+"
        r"(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?)?\s*"
        r"(?:AI\s+)?(?:chips?|GPUs?|compute|servers?|capacity)\s+from\s+(?P<b>" + _EN_TARGET + r")", re.I)),
    # 8-K / 新闻稿常见：「X Closes Acquisition of Y」「X Signs Agreement to Acquire Y」
    ("收购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:closes?|closed|completes?|completed)\s+"
        r"(?:the\s+)?acquisition\s+of\s+(?:100%\s+of\s+)?(?:the\s+)?"
        r"(?P<b>" + _EN_TARGET + r")(?:\s+for\s+(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?))?", re.I)),
    ("收购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:signs?|signed|enters?\s+into|entered\s+into)\s+"
        r"(?:a\s+)?(?:definitive\s+)?(?:agreement|contract)\s+to\s+"
        r"(?:acquire|purchase)\s+(?P<b>" + _EN_TARGET + r")", re.I)),
    # 正文里的无主语英文句：「The Company acquired X」
    ("收购", re.compile(
        r"(?P<a>(?:The|the)\s+Company|Company)\s+(?:acquires?|acquired|to\s+acquire)\s+"
        r"(?P<b>" + _EN_TARGET + r")(?:\s+for\s+(?P<amt>\$?\s?[\d.,]+\s*(?:million|billion|M|B)?))?", re.I)),
    ("合同", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:signs?|signed|enters?\s+into|entered\s+into|announces?)\s+"
        r"(?:a\s+)?(?:definitive\s+)?(?:supply|purchase|services?|manufacturing|lease)\s+"
        r"(?:agreement|contract|deal)\s+with\s+(?P<b>" + _EN_TARGET + r")", re.I)),
    # 「X Announces Plans to Acquire Y」「X Announces Acquisition of Y」
    ("收购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:announces?|announced|unveils?|reveals?)\s+"
        r"(?:plans?|a\s+plan|intent|intention)\s+to\s+(?:acquire|purchase)\s+"
        r"(?P<b>" + _EN_TARGET + r")", re.I)),
    ("收购", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:announces?|announced|completes?|completed|closes?|closed)\s+"
        r"(?:the\s+)?(?:strategic\s+)?acquisition\s+of\s+(?:the\s+)?"
        r"(?P<b>" + _EN_TARGET + r")", re.I)),
    # 「X makes a strategic investment in Y」
    ("投资", re.compile(
        r"(?P<a>" + _EN_ACT + r")\s+(?:makes?|made|announces?|announced)\s+"
        r"(?:a\s+)?(?:strategic\s+)?(?:equity\s+)?(?:investment|stake)\s+in\s+"
        r"(?P<b>" + _EN_TARGET + r")", re.I)),
]

# 英文公告正文里的无主语句：主语就是公告主体（不要求句式里带公司名）
_EN_SUBJECT_RULES = [
    ("收购", r"(?:agreement|plans?|intent)\s+to\s+(?:acquire|purchase)\s+(?P<b>" + _EN_TARGET + r")"),
    ("收购", r"(?:acquisition|purchase)\s+of\s+(?:100%\s+of\s+)?(?:the\s+)?(?P<b>" + _EN_TARGET + r")"),
    ("投资", r"(?:strategic\s+)?(?:equity\s+)?(?:investment|stake)\s+in\s+(?P<b>" + _EN_TARGET + r")"),
    ("收购", r"acquired\s+(?:the\s+)?(?P<b>" + _EN_TARGET + r")\s+(?:business|assets)"),
]

# 英文宾语里的非公司词：its/our/the + 设施名词，不是交易对手
_EN_STOP_START = {"its", "the", "our", "their", "a", "an", "this", "that",
                  "these", "those", "my", "your", "his", "her"}
_EN_TARGET_BAD = {"ai infrastructure", "infrastructure", "data center", "ai factory",
                  "capacity", "compute", "general partner", "limited partner"}


def _en_target_ok(b: str) -> bool:
    t = (b or "").strip().lower()
    if not t:
        return False
    if t.split()[0] in _EN_STOP_START:
        return False
    return t not in _EN_TARGET_BAD


def _trusted_actor(sentence: str, name: str) -> bool:
    """公告标题里的「公司名：」前缀是可信主体，不受后缀表限制。"""
    return bool(re.search(re.escape(name) + r"\s*[:：]", sentence))


_GENERIC_ACTORS_CN = {"公司", "本公司", "该公司", "其", "集团", "母公司", "上市公司",
                      "发行人", "企业", "标的公司"}
_GENERIC_ACTORS_EN = {"the company", "company", "we", "our company", "the issuer",
                      "it", "its", "the buyer", "the seller", "its subsidiary",
                      "the subsidiary", "the registrant"}


def _doc_subject(title: str, source: str) -> str:
    """公告/披露的发行主体：正文里的「公司」指的就是它。

    只在公告/SEC 这类「标题即主体」的源上启用；媒体报道标题是句子，不能这么取。
    """
    t = (title or "").strip()
    if not (source in ("上市公司公告", "东财公告", "美股交易披露(SEC)", "美股披露(SEC)")
            or t.startswith(("[公告]", "[SEC"))):
        return ""
    is_sec = t.startswith("[SEC")
    t = re.sub(r"^\[[^\]]{1,14}\]\s*", "", t).strip()
    if is_sec:
        # [SEC 8-K] Valor Energy Inc  (VAI) / [SEC 8-K] 英伟达 NVDA
        t = re.sub(r"\s*\([A-Za-z.]{1,8}\)\s*$", "", t).strip()
        m = re.match(r"(.+?)\s+[A-Z]{2,6}$", t)
        if m:
            t = m.group(1).strip()
        return t
    m = re.search(r"[（(]([^）)]{2,14})[）)]\s*$", t)   # 研报末尾括号是标的
    if m and not re.search(r"20\d{2}年|\d{1,2}月\d{1,2}日|记录表|公告|报告|注册稿", m.group(1)):
        return m.group(1).strip()
    m = re.match(r"([^：:]{2,24})[：:]", t)
    if m:
        cand = m.group(1).strip()
        if cand and not any(x in cand for x in ("公告", "研报", "快讯", "重磅", "突发")):
            return cand
    if len(t) <= 40 and not re.search(r"[，。；]", t):
        return t
    return ""


def _actor(a: str, subject: str) -> str:
    """「公司/本公司/The Company」用公告标题里的发行主体替换。"""
    a = (a or "").strip()
    if subject and (a in _GENERIC_ACTORS_CN or a.lower() in _GENERIC_ACTORS_EN):
        return subject
    return a


def _structured(sent: str, source: str, subject: str = "") -> list[dict]:
    """「向/对 <目标列表> 投资/采购」「向 <目标> 租给」这类结构。

    真实新闻经常一条句子里有多个投资方/多个标的：
      「台积电向 Anthropic 新股和 OpenAI 优先股投入最高 5 亿美元」
    用固定正则只能抓到第一个，这里把中间那段目标全部展开。
    """
    out: list[dict] = []
    trusted = source in ("上市公司公告", "东财公告", "券商研报")

    # 向/对 <列表> 投入/投资/增资/注资/出资
    for m in re.finditer(r"(?P<a>" + _ACT + r")[^，。；]{0,8}?(?:向|对)(?P<rest>[^，。；]{2,70}?)"
                         r"(?:投入|投资|增资|注资|出资)", sent):
        a_raw, rest = _actor(m.group("a"), subject), m.group("rest")
        amt = _amount_near(sent, m.start(), m.end(), 30)
        targets = [e[2] for e in find_entities(rest)]
        if not targets:
            targets = [re.split(r"[和与、及]", rest)[0]]
        for t in targets[:4]:
            e = _mk(a_raw, t, "出资", amt, sent,
                    trusted=trusted and (_trusted_actor(sent, a_raw) or _trusted_actor(sent, str(t))))
            if e:
                out.append(e)

    # 向/从 <列表> 采购/购买/租用/租赁 → 买方 → 供方
    for m in re.finditer(r"(?P<a>" + _ACT + r")[^，。；]{0,8}?(?:向|从)(?P<rest>[^，。；]{2,70}?)"
                         r"(?:采购|订购|购买|租用|租赁)", sent):
        a_raw, rest = _actor(m.group("a"), subject), m.group("rest")
        amt = _amount_near(sent, m.start(), m.end(), 30)
        targets = [e[2] for e in find_entities(rest)]
        if not targets:
            targets = [re.split(r"[和与、及]", rest)[0]]
        for t in targets[:4]:
            e = _mk(a_raw, t, "采购", amt, sent,
                    trusted=trusted and (_trusted_actor(sent, a_raw) or _trusted_actor(sent, str(t))))
            if e:
                out.append(e)

    # X 租给 Y → Y 是买方/租用方
    for m in re.finditer(r"(?P<a>" + _ACT + r")[^，。；]{0,20}?(?:租给|出租给|供应给)(?P<b>" + _ACT + r")", sent):
        amt = _amount_near(sent, m.start(), m.end(), 30)
        e = _mk(_actor(m.group("b"), subject), _actor(m.group("a"), subject), "租用", amt, sent,
                trusted=trusted and (_trusted_actor(sent, m.group("a")) or _trusted_actor(sent, m.group("b"))))
        if e:
            out.append(e)

    # 采购 <供应商> ... 租给 <买方> → 买方 → 供应商
    for m in re.finditer(r"采购(?P<sup>[^，。；]{2,40}?)"
                         r"[^。；]{0,60}?(?:租给|出租给|供应给)(?P<buyer>" + _ACT + r")", sent):
        sups = [e[2] for e in find_entities(m.group("sup"))]
        amt = _amount_near(sent, m.start(), m.end(), 30)
        for sup in sups[:2]:
            e = _mk(m.group("buyer"), sup, "租用", amt, sent)
            if e:
                out.append(e)
    return out


def _extract_sentence(sent: str, source: str, title: str, subject: str = "") -> list[dict]:
    out: list[dict] = []
    trusted_title = source in ("上市公司公告", "东财公告", "券商研报")

    # ② 句式兜底：未知公司
    for channel, pat in _RULES:
        for m in pat.finditer(sent):
            g = m.groupdict()
            a_raw = _actor(g.get("a") or "", subject)
            b_raw = _actor(g.get("b") or "", subject)
            # 「星火资本领投 克雷西 2023-05-25」这类是文章作者/日期行，不是交易
            if g.get("b") and re.match(r"\s*\d{4}\s*[-/年.]\s*\d{1,2}",
                                       sent[m.end("b"):m.end("b") + 18]):
                continue
            amount = _amount(g.get("amt") or "") or _amount_near(sent, m.start(), m.end(), 36)
            if channel == "融资":
                e = _mk("资本市场", a_raw, channel, amount, sent,
                        trusted=trusted_title and _trusted_actor(sent, a_raw))
                if e:
                    out.append(e)
                continue
            if channel == "IPO":
                # 「上市」也被汽车/手机/CPU 用，必须带资本市场语境，否则是产品发布
                if not re.search(r"IPO|敲钟|挂牌|招股|过会|注册生效|证券交易所|港交所|上交所|"
                                 r"深交所|纳斯达克|纽交所|港股|美股|A股|科创板|创业板|北交所|"
                                 r"发行价|募资|路演|市值|估值|万亿|千亿|百亿", sent):
                    continue
                # IPO 的金额经常是「创始人套现/估值」，不是交易金额，宁可不填。
                # 未知主体必须带公司后缀：否则「美国重大IPO」「相对于IPO」会被当成公司名。
                e = _mk("资本市场", a_raw, channel, "", sent,
                        trusted=trusted_title and _trusted_actor(sent, a_raw),
                        allow_bare_to=False)
                if e:
                    out.append(e)
                continue
            if channel == "自建":
                what = g.get("what") or "数据中心"
                e = _mk(a_raw, "自建" + what.replace(" ", ""), channel, amount, sent,
                        trusted=trusted_title and _trusted_actor(sent, a_raw))
                if e:
                    out.append(e)
                continue
            if not a_raw or not b_raw:
                continue
            e = _mk(a_raw, b_raw, channel, amount, sent,
                    trusted=trusted_title and (_trusted_actor(sent, a_raw) or _trusted_actor(sent, b_raw)))
            if e:
                out.append(e)

    # ②b 公告正文的无主语句：「拟向X增资」「拟收购X」「与X签署合同」
    if subject:
        for m in re.finditer(r"(?:拟|将|计划|同意|决定)?(?:向|对)(?P<rest>[^，。；]{2,50}?)"
                             r"(?:增资|注资|出资|投资|股权投资)", sent):
            rest = re.sub(r"^(?:全资|控股|参股|下属)?子公司", "", m.group("rest").strip())
            amt = _amount_near(sent, m.start(), m.end(), 30)
            targets = [e[2] for e in find_entities(rest)]
            if not targets:
                targets = [re.split(r"[和与、及]|进行|用于|认购|以", rest)[0]]
            for t in targets[:4]:
                e = _mk(subject, t, "出资", amt, sent, trusted=True, allow_bare_to=True)
                if e:
                    out.append(e)

        for m in re.finditer(r"(?<!被)(?:拟|将|计划|同意)?(?:以[^，。；]{0,12})?收购"
                             r"(?P<b>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·\-]{1,14})", sent):
            b = re.sub(r"\d+(?:\.\d+)?%.*$", "", m.group("b"))
            amt = _amount_near(sent, m.start(), m.end(), 30)
            e = _mk(subject, b, "收购", amt, sent, trusted=True, allow_bare_to=True)
            if e:
                out.append(e)

        for m in re.finditer(r"(?:与|同)(?P<b>[^，。；]{2,40}?)(?:签署|签订|订立)"
                             r"[^，。；]{0,20}?(?:合同|协议|订单)", sent):
            b = m.group("b")
            amt = _amount_near(sent, m.start(), m.end(), 36)
            e = _mk(subject, b, "合同", amt, sent, trusted=True, allow_bare_to=True)
            if e:
                out.append(e)

        if re.search(r"首次公开发行|(?<![A-Za-z])IPO(?![A-Za-z])|敲钟", sent):
            e = _mk("资本市场", subject, "IPO", "", sent, trusted=True)
            if e:
                out.append(e)

    # ②c 英文公告无主语句：主语 = 公告主体（Dropbox / XMax / Quanome 这类 8-K）
    if subject:
        for channel, pat in _EN_SUBJECT_RULES:
            for m in re.finditer(pat, sent, re.I):
                if not _en_target_ok(m.group("b")):
                    continue
                e = _mk(subject, m.group("b"), channel,
                        _amount_near(sent, m.start(), m.end(), 36), sent,
                        trusted=True, allow_bare_to=False)
                if e:
                    out.append(e)

    # ③ 英文句式
    for channel, pat in _EN_RULES:
        for m in pat.finditer(sent):
            g = m.groupdict()
            if g.get("b") and not _en_target_ok(g["b"]):
                continue
            a_raw = _actor(g.get("a") or "", subject)
            b_raw = _actor(g.get("b") or "", subject)
            amount = _amount(g.get("amt") or "") or _amount_near(sent, m.start(), m.end(), 36)
            if channel == "融资":
                investor = b_raw or "资本市场"
                e = _mk(investor, a_raw, channel, amount, sent)
            else:
                e = _mk(a_raw, b_raw, channel, amount, sent)
            if e:
                out.append(e)

    # ④ 结构化的「向/对 ... 投资/采购」多标的
    out.extend(_structured(sent, source, subject))
    return out


def _split_sentences(text: str) -> list[str]:
    # 英文新闻稿/公告标题常是硬换行（「GridAI Technologies\nSigns ...」），
    # 先把换行并成空格，否则主语会被切掉、英文句式全部失效。
    text = re.sub(r"[ \t]*\r?\n[ \t]*", " ", text or "")
    parts = re.split(r"[。！？!?；;]+", text)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) < 8:
            continue
        if len(p) > 180:
            out.extend(x.strip() for x in re.split(r"[，,]", p) if len(x.strip()) >= 8)
        else:
            out.append(p)
    return out


def _doc_text(row) -> str:
    title = row["title"] if "title" in row.keys() else ""
    summary = row["summary"] if "summary" in row.keys() else ""
    content = row["content"] if "content" in row.keys() else ""
    if content and re.match(r"\s*%PDF", content or ""):
        content = ""
    if content and content.count("\x00") > 20:
        content = ""
    text = f"{title}。{summary}。{content[:20000]}"
    return re.sub(r"[ \t\u3000]+", " ", text)


# 只把「听得懂的公司」放到图上。
# 规则抽取会带出大量地方小厂、壳公司、英文生僻名（如「沛塬电子」「Quanome
# Technologies」），对看板读者没有意义。这里用规范名白名单做最后一道过滤：
# 两端都在白名单内的边才保留，宁可少几条，也不让桑基图变成生僻公司名录。
PROMINENT: set[str] = {
    # 市场（允许作为资金来源/去向）
    "资本市场",
    # 模型 / 应用
    "OpenAI", "Anthropic", "xAI", "Google DeepMind", "Google", "Meta",
    "Mistral AI", "Cohere", "Perplexity", "Safe Superintelligence",
    "Thinking Machines", "智谱", "月之暗面", "MiniMax", "百川智能",
    "零一万物", "阶跃星辰", "DeepSeek", "阿里巴巴", "字节跳动", "腾讯",
    "百度", "科大讯飞", "商汤", "苹果", "特斯拉", "小米", "荣耀",
    "微软", "Salesforce", "SAP", "IBM",
    # 芯片 / 代工
    "英伟达", "AMD", "英特尔", "博通", "高通", "Marvell", "Arm", "台积电",
    "三星电子", "SK海力士", "美光", "中芯国际", "华虹半导体", "长江存储",
    "长鑫存储", "寒武纪", "海光信息", "摩尔线程", "壁仞科技", "燧原科技",
    "沐曦集成", "华为", "阿斯麦", "工业富联", "立讯精密", "浪潮信息",
    "中科曙光", "超微", "戴尔", "联想",
    # 云 / 算力 / 数据中心
    "阿里云", "腾讯云", "华为云", "火山引擎", "百度智能云", "金山云",
    "天翼云", "移动云", "微软云", "AWS", "谷歌云", "甲骨文云", "甲骨文",
    "亚马逊", "CoreWeave", "Lambda", "Crusoe", "Together AI", "Groq",
    "Cerebras", "中国移动", "中国电信", "中国联通", "世纪互联", "万国数据",
    "润泽科技", "秦淮数据",
    # 资本 / 投行
    "红杉资本", "高瓴资本", "软银", "淡马锡", "GIC", "沙特公共投资基金",
    "MGX", "阿布扎比投资局", "穆巴达拉", "贝莱德", "黑石", "凯雷", "KKR",
    "华平投资", "春华资本", "a16z", "Founders Fund", "Thrive Capital",
    "老虎环球", "Coatue", "光速创投", "General Catalyst", "Index Ventures",
    "Benchmark", "Accel", "NEA", "Khosla Ventures", "GV",
    "Salesforce Ventures", "M12", "英特尔资本", "高通创投",
    "摩根士丹利", "高盛", "摩根大通", "美国银行", "花旗", "汇丰", "瑞银",
    "巴克莱", "德意志银行", "中信证券", "中金公司", "深创投", "中金资本",
    "招银国际", "国家集成电路产业投资基金",
}

_CACHE: dict[tuple, tuple[list[dict], dict]] = {}

_EN_CORP_TAIL = re.compile(
    r"(?i)(?:[,\s]+(?:inc|corp|corporation|ltd|llc|limited|plc|nv|ag|co))\.?$")


def _simp_key(s: str) -> str:
    """同一家公司常写成「QAI Labs」/「QAI Labs Ltd.」，聚合时按去掉公司后缀归一"""
    t = re.sub(r"[\s,]+$", "", (s or "").strip())
    for _ in range(2):
        t2 = _EN_CORP_TAIL.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return t.lower()


def extract(rows, max_edges: int = 20, cache_key: tuple | None = None) -> tuple[list[dict], dict]:
    """从 doc 行抽边。rows 需含 id/title/summary/content/source_name/t_valid/url。"""
    if cache_key is not None and cache_key in _CACHE:
        return _CACHE[cache_key]

    agg: dict[tuple, dict] = {}
    doc_count = 0
    for row in rows:
        text = _doc_text(row)
        if not text.strip():
            continue
        doc_count += 1
        seen_in_doc = set()
        source_name = row["source_name"] or ""
        subject = _doc_subject(row["title"] or "", source_name)
        for sent in _split_sentences(text):
            for e in _extract_sentence(sent, source_name, row["title"] or "", subject):
                key = (_simp_key(e["from"]), _simp_key(e["to"]), e["channel"])
                if key in seen_in_doc:
                    continue
                seen_in_doc.add(key)
                rec = agg.get(key)
                if not rec:
                    rec = {"from": e["from"], "to": e["to"], "channel": e["channel"],
                           "amount": "", "n": 0, "date": "", "source": "",
                           "from_layer": e["from_layer"], "to_layer": e["to_layer"],
                           "docs": []}
                    agg[key] = rec
                rec["n"] += 1
                if e["amount"] and (not rec["amount"] or len(e["amount"]) > len(rec["amount"])):
                    rec["amount"] = e["amount"]
                d = (row["t_valid"] or "")[:10]
                if d > rec["date"]:
                    rec["date"] = d
                if not rec["source"]:
                    rec["source"] = row["source_name"] or ""
                if len(rec["docs"]) < 3:
                    rec["docs"].append({
                        "id": row["id"], "title": (row["title"] or "")[:90],
                        "url": row["url"] or "", "source": row["source_name"] or "",
                        "date": d, "quote": e["quote"],
                    })

    # 只保留两端都在知名主体白名单内的边（过滤地方小厂/壳公司/生僻英文名）
    edges = [rec for rec in agg.values()
             if rec["from"] in PROMINENT and rec["to"] in PROMINENT]
    for rec in edges:
        # 置信度：双方都是已知主体 > 一方已知 > 未知
        known = (rec["from_layer"] != "其他") + (rec["to_layer"] != "其他")
        rec["confidence"] = 1.0 + 0.5 * known
        rec["_score"] = rec["n"] * 2 + rec["confidence"] + (1 if rec["amount"] else 0)
    edges.sort(key=lambda r: (r["_score"], r["n"], r["date"]), reverse=True)
    total = len(edges)
    shown = edges[:max_edges]
    for r in shown:
        r.pop("_score", None)

    meta = {
        "total": total, "shown": len(shown), "docs_scanned": doc_count,
        "sources": sorted({r["source"] for r in shown if r["source"]}),
        "note": "免费公开源 · 规则从全文抽取；金额为原文口径，未做汇率换算",
    }
    result = (shown, meta)
    if cache_key is not None:
        _CACHE.clear()
        _CACHE[cache_key] = result
    return result
