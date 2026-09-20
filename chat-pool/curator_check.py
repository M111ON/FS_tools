"""curator_check.py — every CURATOR_RULES rule must catch a real message.

Each rule in pool.py has a test function. If that function silently breaks
(e.g. regex typo, wrong boundary), the rule goes dead — the screen shows
a code name like `hex-blob 3` instead of the Thai word, and no other test
catches it because pool.py demo only tests the CURATOR pipeline, not that
each individual rule actually fires.

This test feeds a hand-crafted message to each rule's test function and
verifies it returns True.  It also verifies that a REAL message (not noise)
is NOT caught — proving the rule has shape, not just a blanket match.

    python curator_check.py

Exit 0 = every rule fires on its intended input and spares real text.
"""
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
import pool

# (rule name, message that SHOULD match, message that should NOT match)
# Each SHOULD-match is the simplest string that proves the rule works.
# Each SHOULDN'T-match is a real chat message that must not be noise.
TEST_CASES = [
    ("test-word",   "test",         "การทดสอบจบแล้ว"),
    ("test-word",   "ping",         "ping ได้ไหม"),
    ("test-word",   "hello",        "hello world ทดสอบ"),
    ("test-probe",  "test: ok",     "test case ที่ 5"),
    ("test-probe",  "probe=1",      "probe ตัวนี้ทำงาน"),
    ("stub",        "…",            "ได้เลย"),
    ("stub",        "..",           "ok ครับ"),
    ("ack",         "got it",       "ได้เลย ขอบคุณ"),
    ("ack",         "รับทราบ",      "รับทราบแล้วจะทำต่อ"),
    ("ack",         "ok",           "ok ครับ จะลองดู"),
    ("ack",         "ครับ",         "ครับ ผมเข้าใจ"),
    ("sess-id",     "ses_abc123def456gh", "session ใหม่"),
    ("hex-blob",    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "ข้อความทั่วไป"),
    ("b64-blob",    "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkwYWJjZGVmZ2hpamtsbW5vcA==", "base64 encode"),
    ("punct-soup",  "...!!!---",    "ข้อความทั่วไป"),
    ("punct-soup",  ";;;:::'''",    "เครื่องหมาย"),
    ("loud",        "AAAAAAAAAAAAAAAAAAAA",  "ข้อความปกติ"),
]


def _rule_test(name):
    """Get the test function for a rule by name."""
    for rname, _tag, _weight, test in pool.CURATOR_RULES:
        if rname == name:
            return test
    return None


def run():
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-32s %s" % ("PASS" if ok else "FAIL", name, detail))

    # --- every rule has at least one test case ---
    rule_names = {r[0] for r in pool.CURATOR_RULES}
    tested = set()
    for rule_name, should_match, should_not in TEST_CASES:
        test = _rule_test(rule_name)
        tested.add(rule_name)

        if test is None:
            check("rule-exists-%s" % rule_name, False,
                  "rule not found in CURATOR_RULES")
            continue

        # should match
        try:
            hit = test(should_match)
        except Exception as e:
            hit = False
        check("catches-%s" % rule_name, bool(hit),
              "input=%r -> %s" % (should_match, hit))

        # should NOT match a real message
        try:
            miss = test(should_not)
        except Exception as e:
            miss = False
        check("spares-real-%s" % rule_name, not bool(miss),
              "input=%r -> %s (should be False)" % (should_not, miss))

    # --- every rule is tested ---
    untested = rule_names - tested
    check("all-rules-tested", not untested,
          "untested: %s" % sorted(untested) if untested else "")

    # --- curator_hits works end-to-end with a known message ---
    msg = {"text": "AAAAAAAAAAAAAAAAAAAA"}
    score, tags = pool.curator_hits(msg)
    check("curator_hits-fires", score > 0 and "loud" in tags,
          "score=%d tags=%s" % (score, tags))

    # --- curator_hits spares a real message ---
    msg2 = {"text": "การทดสอบระบบเสร็จแล้วครับ ลองรันดูนะ"}
    score2, tags2 = pool.curator_hits(msg2)
    check("curator_hits-spare-real", score2 == 0,
          "score=%d tags=%s (should be 0)" % (score2, tags2))

    bad = [n for n, ok, _ in results if not ok]
    total = len(results)
    passed = total - len(bad)
    print("\ncurator_check: %d/%d PASS%s" % (
        passed, total,
        "" if not bad else " — FAILED: " + ", ".join(bad[:5])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
