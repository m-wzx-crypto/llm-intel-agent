"""Ablation：用 golden set 量化「实体消歧」到底值多少。

三种配置：
  A full       —— 别名表 + 最长匹配 + 跨度覆盖 + 标题优先（当前线上逻辑）
  B no_span    —— 有别名表，但退化成朴素子串并集（不做跨度覆盖）
  C no_alias   —— 没有别名表，只有按板块的关键词列表（v0.5 的做法）

指标：
  板块准确率   category == 标注
  实体召回率   must ⊆ 预测
  误报率       标注里的 not 出现在预测里

跑法：python3 src/ablation.py     （或 GET /api/ablation）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entities as ENT
import extract
from sources import CATEGORIES, CATEGORY_PRIORITY, ENTITIES as CAT_WORDS

def _golden_path() -> str:
    """打包后 golden_set.json 在 _MEIPASS/tests/；开发时在 ../tests/"""
    if getattr(sys, "frozen", False):
        for cand in (os.path.join(sys._MEIPASS, "tests", "golden_set.json"),
                     os.path.join(sys._MEIPASS, "golden_set.json")):
            if os.path.exists(cand):
                return cand
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "tests", "golden_set.json")


GOLDEN = _golden_path()


def _cat_of(cats: dict) -> str:
    if not cats:
        return "行业其他"
    return sorted(cats.keys(), key=lambda c: (-len(cats[c]), CATEGORY_PRIORITY.get(c, 9)))[0]


# ── 配置 A：**直接调用生产代码** extract.classify（不是复刻，避免 ablation 与线上漂移）──
def predict_full(title: str) -> dict:
    c = extract.classify(title, "")
    return {"entities": list(c["entities"]), "category": c["category"]}


# ── 配置 B：有别名但无跨度覆盖（朴素子串并集）──
def predict_no_span(title: str) -> dict:
    low = title.lower()
    hit_alias = [a for a in ENT.ALIASES if a.alias.lower() in low]
    ents = sorted({a.canonical for a in hit_alias})
    cats: dict[str, set] = {}
    for a in hit_alias:
        cats.setdefault(a.category, set()).add(a.canonical)
    return {"entities": ents, "category": _cat_of(cats)}


# ── 配置 C：无别名表，只有板块关键词（v0.5）──
def predict_no_alias(title: str) -> dict:
    blob = title
    cats: dict[str, set] = {}
    for cat, words in CAT_WORDS.items():
        got = [w for w in words if w.lower() in blob.lower()]
        if got:
            cats[cat] = set(got)
    ents = sorted({w for ws in cats.values() for w in ws})
    return {"entities": ents, "category": _cat_of(cats)}


def score(predictor, golden) -> dict:
    n = len(golden)
    cat_ok = ent_ok = 0
    false_pos = 0
    misses = []
    for g in golden:
        p = predictor(g["title"])
        if p["category"] == g["category"]:
            cat_ok += 1
        must = set(g.get("must") or [])
        ok_ent = must.issubset(set(p["entities"]))
        if ok_ent:
            ent_ok += 1
        bad = set(g.get("not") or []) & set(p["entities"])
        if bad:
            false_pos += 1
        if not (ok_ent and p["category"] == g["category"]):
            misses.append({"title": g["title"][:36], "want_cat": g["category"],
                           "got_cat": p["category"], "want_ent": sorted(must),
                           "got_ent": p["entities"][:5], "误报": sorted(bad)})
    return {
        "n": n,
        "category_acc": round(cat_ok / n, 3),
        "entity_recall": round(ent_ok / n, 3),
        "false_pos_rate": round(false_pos / n, 3),
        "misses": misses[:8],
    }


def run() -> dict:
    if not os.path.exists(GOLDEN):
        raise RuntimeError(f"golden set 未随包分发：{GOLDEN}")
    golden = json.load(open(GOLDEN, encoding="utf-8"))
    return {
        "golden_size": len(golden),
        "configs": {
            "A_full":     score(predict_full, golden),
            "B_no_span":  score(predict_no_span, golden),
            "C_no_alias": score(predict_no_alias, golden),
        },
    }


if __name__ == "__main__":
    r = run()
    print(f"golden set: {r['golden_size']} 条\n")
    print(f"{'配置':<12}{'板块准确':>10}{'实体召回':>10}{'误报率':>10}")
    for k, v in r["configs"].items():
        print(f"{k:<12}{v['category_acc']:>10}{v['entity_recall']:>10}{v['false_pos_rate']:>10}")
    print("\nA 配置的漏判明细：")
    for m in r["configs"]["A_full"]["misses"]:
        print(f"  · {m['title']}")
        print(f"    期望 [{m['want_cat']}] {m['want_ent']} → 实际 [{m['got_cat']}] {m['got_ent']}")
