"""实体消歧回归测试。

起因：2026-11-23 发现「北美豆包」被归到字节豆包，实际指 Gemini。
每次改别名表后跑一遍：python3 tests/test_entities.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import entities

CASES = [
    # (文本, 必须包含, 必须不包含)
    ("「北美豆包」又发新模型，编程解题率升至三倍多", "Gemini", "豆包"),
    ("美国豆包 Gemini 更新到 3.0", "Gemini", None),
    ("美版豆包开始支持长上下文", "Gemini", "豆包"),
    ("「北美大豆包」谷歌，终于杀回 AI 牌桌中央", "Gemini", "豆包"),
    ("美国大豆包又发新模型", "Gemini", "豆包"),
    ("字节豆包日活破亿", "豆包", None),
    ("豆包工作接入 Kimi、GLM、DeepSeek", "Kimi", None),
    ("英伟达 Blackwell 对华销售", "英伟达", None),
    ("中芯国际 7nm 产能爬坡", "中芯", None),
    ("宇树科技发布新一代人形机器人", "宇树", None),
    ("智元机器人完成新一轮融资", "智元", None),
    ("月之暗面回应裁员传闻", "Kimi", None),
    ("智谱 GLM-5 发布", "智谱", None),
]

def main():
    bad = 0
    for text, must, must_not in CASES:
        r = entities.resolve(text)
        ents = r["entities"]
        ok = must in ents and (must_not is None or must_not not in ents)
        if not ok:
            bad += 1
        print(("PASS " if ok else "FAIL ") + text[:30] + "  →  " + "/".join(ents))
    print(f"\n{len(CASES)-bad}/{len(CASES)} passed")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
