"""End-to-end API tests for the transcript typo batch-correction workbench.

Run with: python3 test_typos_api.py  (or via pytest).
"""
import os
import shutil
import tempfile

import pytest

os.environ.setdefault("STATIC_DIR", "/nonexistent")
_TMP = tempfile.mkdtemp(prefix="typo-api-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.typos import (  # noqa: E402
    correct_text,
    normalize_pairs,
    plan_correction,
    transcript_version,
)

DOC = {
    "segments": [
        {"id": "s1", "start": 0, "end": 5, "speaker": "李明",
         "text": "我在用计算机做计算，计算真方便"},
        {"id": "s2", "start": 5, "end": 8, "speaker": "老李",
         "text": "嗯"},
        {"id": "s3", "start": 8, "end": 12, "speaker": "王主任",
         "text": "计算所的计算机很旧"},
    ],
    "annotations": [
        {"id": "a1", "start": 0, "end": 5, "speaker": "李明",
         "topic": "背景", "note": "备注一",
         "source": "rule", "ruleId": "rule-x", "ruleSlot": 0},
    ],
}


# ---------- engine ----------

def test_normalize_pairs_validation():
    assert normalize_pairs([]) == []
    # completely empty rows are ignored
    assert normalize_pairs([{"from": "", "to": "  "}]) == []
    # surrounding whitespace is stripped
    assert normalize_pairs([{"from": " 错 ", "to": " 对 "}]) == \
        [{"from": "错", "to": "对"}]

    for bad, needle in (
        ([{"from": "  ", "to": "对"}], "未填写错词"),
        ([{"from": "错", "to": ""}], "正词为空"),
        ([{"from": "错", "to": "甲"},
          {"from": "错", "to": "乙"}], "多条校正"),
    ):
        with pytest.raises(ValueError) as exc:
            normalize_pairs(bad)
        assert needle in str(exc.value)


def test_longest_pair_wins_at_same_position():
    res = correct_text("用计算机做计算",
                       [{"from": "计算", "to": "运算"},
                        {"from": "计算机", "to": "电脑"}])
    assert res["text"] == "用电脑做运算"
    assert [(h["from"], h["start"], h["end"]) for h in res["hits"]] == \
        [("计算机", 1, 4), ("计算", 5, 7)]


def test_single_pass_replacement_is_not_rematched():
    # a->b and b->c must not chain: replaced text is never scanned again
    res = correct_text("aaa", [{"from": "a", "to": "b"},
                               {"from": "b", "to": "c"}])
    assert res["text"] == "bbb"
    assert len(res["hits"]) == 3
    assert all(h["from"] == "a" for h in res["hits"])


def test_original_text_scanned_once_per_segment():
    # non-overlapping hits advance past the match; no overlap double counts
    res = correct_text("计算计算", [{"from": "计算", "to": "核算"}])
    assert res["text"] == "核算核算"
    assert [(h["start"], h["end"]) for h in res["hits"]] == [(0, 2), (2, 4)]


def test_equal_pair_and_case_sensitivity():
    # wrong == correct never produces a hit (it participates in nothing)
    res = correct_text("计算机", [{"from": "计算机", "to": "计算机"}])
    assert res["text"] == "计算机"
    assert res["hits"] == []
    # ...and it does not shield a distinct shorter pair at the same position
    res = correct_text("计算机", [{"from": "计算机", "to": "计算机"},
                                  {"from": "计算", "to": "运算"}])
    assert res["text"] == "运算机"
    assert [h["from"] for h in res["hits"]] == ["计算"]

    res_cs = correct_text("AI ai", [{"from": "ai", "to": "人工智能"}],
                          case_sensitive=True)
    assert res_cs["text"] == "AI 人工智能"
    res_ci = correct_text("AI ai", [{"from": "ai", "to": "人工智能"}],
                          case_sensitive=False)
    assert res_ci["text"] == "人工智能 人工智能"
    assert len(res_ci["hits"]) == 2


def test_plan_correction_summaries():
    plan = plan_correction(DOC, [
        {"from": "计算机", "to": "电脑"},
        {"from": "计算", "to": "核算"},
    ])
    ids = [s["id"] for s in plan["segments"]]
    assert ids == ["s1", "s3"]  # s2 has no hit and is omitted
    s1 = plan["segments"][0]
    assert s1["originalText"] == "我在用计算机做计算，计算真方便"
    assert s1["correctedText"] == "我在用电脑做核算，核算真方便"
    assert [(h["from"], h["to"]) for h in s1["hits"]] == \
        [("计算机", "电脑"), ("计算", "核算"), ("计算", "核算")]
    assert s1["speaker"] == "李明" and (s1["start"], s1["end"]) == (0, 5)
    by_from = {p["from"]: p for p in plan["pairs"]}
    assert by_from["计算机"]["hits"] == 2 and by_from["计算机"]["segments"] == 2
    assert by_from["计算"]["hits"] == 3 and by_from["计算"]["segments"] == 2
    assert plan["summary"] == {"pairs": 2, "hitPairs": 2,
                               "hits": 5, "segments": 2}

    # changing text, speaker or times changes the version; annotations do not
    changed = {"segments": DOC["segments"],
               "annotations": [{"id": "x", "start": 0, "end": 1}]}
    assert transcript_version(changed) == transcript_version(DOC)
    edited = {"segments": [dict(s) for s in DOC["segments"]],
              "annotations": DOC["annotations"]}
    edited["segments"][0] = dict(edited["segments"][0], text="改过了")
    assert transcript_version(edited) != transcript_version(DOC)
    reimported = {"segments": [dict(s, id="new-id") for s in DOC["segments"]],
                  "annotations": DOC["annotations"]}
    assert transcript_version(reimported) != transcript_version(DOC)


# ---------- API ----------

def test_api():
    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        p = c.post("/api/projects", json={"name": "错词校正访谈"}).json()
        pid = p["id"]
        c.put(f"/api/projects/{pid}",
              json={"name": "错词校正访谈", "document": DOC})

        # preview
        r = c.post(f"/api/projects/{pid}/typo-corrections/preview",
                   json={"pairs": [
                       {"from": "计算机", "to": "电脑"},
                       {"from": "计算", "to": "核算"}]})
        assert r.status_code == 200, r.text
        data = r.json()
        version = data["version"]
        assert version.startswith("segv-")
        assert [s["id"] for s in data["segments"]] == ["s1", "s3"]
        assert data["segments"][0]["correctedText"] == \
            "我在用电脑做核算，核算真方便"
        assert data["summary"] == {"pairs": 2, "hitPairs": 2,
                                   "hits": 5, "segments": 2}

        # invalid pairs are blocked before any write
        assert c.post(
            f"/api/projects/{pid}/typo-corrections/preview",
            json={"pairs": [{"from": "计算机", "to": ""}]}
        ).status_code == 400
        assert c.post(
            f"/api/projects/{pid}/typo-corrections/preview",
            json={"pairs": [{"from": "计算机", "to": "电脑"},
                            {"from": "计算机", "to": "机器"}]}
        ).status_code == 400
        assert c.post("/api/projects/nope/typo-corrections/preview",
                      json={"pairs": []}).status_code == 404
        # nothing changed after the blocked attempts
        still = c.get(f"/api/projects/{pid}").json()
        assert still["document"]["segments"][0]["text"] == DOC["segments"][0]["text"]

        # commit without any valid pair -> 400
        assert c.post(f"/api/projects/{pid}/typo-corrections",
                      json={"pairs": [], "version": version}
                      ).status_code == 400

        # commit
        r = c.post(f"/api/projects/{pid}/typo-corrections",
                   json={"pairs": [
                       {"from": "计算机", "to": "电脑"},
                       {"from": "计算", "to": "核算"}],
                       "version": version})
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["counts"] == {"segments": 2, "hits": 5, "pairs": 2}
        saved = result["project"]["document"]
        segs = {s["id"]: s for s in saved["segments"]}
        # only text changed; ids, times, speakers preserved
        assert segs["s1"]["text"] == "我在用电脑做核算，核算真方便"
        assert segs["s3"]["text"] == "核算所的电脑很旧"
        assert segs["s2"]["text"] == "嗯"
        assert (segs["s1"]["start"], segs["s1"]["end"],
                segs["s1"]["speaker"]) == (0, 5, "李明")
        # all annotations preserved untouched, incl. rule provenance
        ann = saved["annotations"][0]
        assert ann["id"] == "a1"
        assert ann["note"] == "备注一"
        assert ann["ruleId"] == "rule-x" and ann["ruleSlot"] == 0
        assert ann["source"] == "rule"


def test_commit_rejects_stale_preview():
    """Editing / re-importing the saved transcript invalidates the preview."""
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"name": "版本冲突"}).json()
        pid = p["id"]
        c.put(f"/api/projects/{pid}",
              json={"name": "版本冲突", "document": DOC})
        preview = c.post(
            f"/api/projects/{pid}/typo-corrections/preview",
            json={"pairs": [{"from": "计算机", "to": "电脑"}]}).json()
        version = preview["version"]

        # someone edits and saves the transcript in the meantime
        edited = {"segments": [dict(s) for s in DOC["segments"]],
                  "annotations": DOC["annotations"]}
        edited["segments"][0]["text"] = "已人工修改：计算机"
        c.put(f"/api/projects/{pid}",
              json={"name": "版本冲突", "document": edited})

        r = c.post(f"/api/projects/{pid}/typo-corrections",
                   json={"pairs": [{"from": "计算机", "to": "电脑"}],
                         "version": version})
        assert r.status_code == 409
        assert "重新预览" in r.json()["detail"]
        # the rejected commit wrote nothing
        after = c.get(f"/api/projects/{pid}").json()
        assert after["document"]["segments"][0]["text"] == \
            "已人工修改：计算机"

        # re-preview against the new transcript, then commit succeeds
        preview2 = c.post(
            f"/api/projects/{pid}/typo-corrections/preview",
            json={"pairs": [{"from": "计算机", "to": "电脑"}]}).json()
        r = c.post(f"/api/projects/{pid}/typo-corrections",
                   json={"pairs": [{"from": "计算机", "to": "电脑"}],
                         "version": preview2["version"]})
        assert r.status_code == 200, r.text
        final = c.get(f"/api/projects/{pid}").json()
        assert final["document"]["segments"][0]["text"] == \
            "已人工修改：电脑"


def test_annotation_only_edit_does_not_invalidate_preview():
    """Changing annotations while the transcript is untouched stays valid."""
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"name": "仅改校注"}).json()
        pid = p["id"]
        c.put(f"/api/projects/{pid}",
              json={"name": "仅改校注", "document": DOC})
        preview = c.post(
            f"/api/projects/{pid}/typo-corrections/preview",
            json={"pairs": [{"from": "计算机", "to": "电脑"}]}).json()

        edited = {"segments": DOC["segments"],
                  "annotations": DOC["annotations"] + [
                      {"id": "a2", "start": 5, "end": 8,
                       "speaker": "老李", "topic": "新增", "note": ""}]}
        c.put(f"/api/projects/{pid}",
              json={"name": "仅改校注", "document": edited})

        r = c.post(f"/api/projects/{pid}/typo-corrections",
                   json={"pairs": [{"from": "计算机", "to": "电脑"}],
                         "version": preview["version"]})
        assert r.status_code == 200, r.text
        saved = r.json()["project"]["document"]
        # the newly added annotation survived the commit
        assert {a["id"] for a in saved["annotations"]} == {"a1", "a2"}
        assert [s["text"] for s in saved["segments"]][0] == \
            "我在用电脑做计算，计算真方便"


def run():
    test_normalize_pairs_validation()
    test_longest_pair_wins_at_same_position()
    test_single_pass_replacement_is_not_rematched()
    test_original_text_scanned_once_per_segment()
    test_equal_pair_and_case_sensitivity()
    test_plan_correction_summaries()
    test_api()
    test_commit_rejects_stale_preview()
    test_annotation_only_edit_does_not_invalidate_preview()
    print("ALL TYPO CORRECTION TESTS PASSED")


if __name__ == "__main__":
    run()
    shutil.rmtree(_TMP, ignore_errors=True)
