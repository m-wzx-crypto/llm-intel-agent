"""混合检索（BM25 + 字符 n-gram + RRF）冒烟测试。"""
import os
import sys
import tempfile

os.environ["LLMINTEL_HOME"] = tempfile.mkdtemp(prefix="llmintel-retrieval-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db          # noqa: E402
import retrieval   # noqa: E402


def _seed():
    db.init_db()
    conn = db.connect()
    rows = [
        ("宇树科技人形机器人完成新一轮融资", "宇树科技宣布完成数亿元融资，人形机器人量产在即。",
         "宇树", "资本事件"),
        ("智谱发布 GLM-5，代码能力提升", "智谱发布新一代大模型 GLM-5，重点提升代码与多模态能力。",
         "智谱", "智谱·唐杰"),
        ("英伟达对华销售新款 GPU", "英伟达表示将在合规范围内向中国客户销售新款加速卡。",
         "英伟达", "算力供应链"),
        ("DeepSeek 开源新模型", "DeepSeek 开源推理模型，价格进一步下探。",
         "DeepSeek", "竞对动态"),
    ]
    now = db.now_iso()
    for i, (title, summary, ents, cat) in enumerate(rows):
        cur = conn.execute(
            "INSERT INTO doc (guid,url,source_id,source_name,title,t_valid,t_txn,content,fetched_ok)"
            " VALUES (?,?,?,?,?,?,?,?,1)",
            (f"g{i}", f"https://example.com/{i}", "t", "测试源", title, now, now, summary))
        conn.execute(
            "INSERT INTO item (doc_id,category,score,keywords,summary,entities,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cur.lastrowid, cat, 1.0, "[]", summary, '["' + ents + '"]', now))
    conn.commit()
    conn.close()


def main():
    _seed()
    hits = retrieval.retrieve("宇树 机器人 融资", k=5)
    assert hits, "no hits"
    assert hits[0]["title"].startswith("宇树"), hits[0]["title"]
    assert hits[0]["signals"], "missing signals"
    hits2 = retrieval.retrieve("大模型 代码能力", k=5)
    assert hits2 and "GLM" in hits2[0]["title"], hits2[:1]
    hits3 = retrieval.retrieve("GPU 加速卡", k=5)
    assert hits3 and "英伟达" in hits3[0]["title"]
    assert retrieval.retrieve("", 5) == []
    print("PASS retrieval top1:", hits[0]["title"][:24])
    print("PASS retrieval GLM :", hits2[0]["title"][:24])
    print("PASS retrieval GPU :", hits3[0]["title"][:24])
    return 0


if __name__ == "__main__":
    sys.exit(main())
