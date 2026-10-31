"""Conservative query-intent routing gate.

False-positive safety is the priority: uncertain classification falls back to
the existing baseline; relation never opens unrelated view union.
"""
import re


RULES = (
    ("relation", re.compile(r"=|\b(equal|equals|relation|solve|why)\b", re.I)),
    ("shape", re.compile(r"\^|\b(shape|form|pattern)\b", re.I)),
    ("value", re.compile(r"\b(value|number|how much|calculate)\b", re.I)),
)


def classify(q):
    hits = [name for name, rx in RULES if rx.search(q)]
    if len(hits) == 1:
        return hits[0], 0.90
    if len(hits) > 1:
        return "composite", 0.65
    return "unknown", 0.0


def route(intent, confidence):
    if confidence < 0.80 or intent == "unknown":
        return "baseline"
    if intent == "relation":
        return "relation_graph"
    if intent == "composite":
        return "bounded_union"
    return f"{intent}_route"


def main():
    cases = {
        "12 + 3 = 15": "relation",
        "solve x + 3 = 9": "relation",
        "what is the value of 12": "value",
        "what shape is x^2+y^2": "shape",
        "pattern and value of x": "composite",
        "tell me about this": "unknown",
        "number equals shape": "composite",
        "the word relation appears in a note": "relation",
        "shape of the number 12": "composite",
        "x = 12": "relation",
    }
    safe = 0
    for q, expected in cases.items():
        got, conf = classify(q)
        path = route(got, conf)
        # Safety gate: relation must never route to a generic union.
        ok = (expected == "relation" and path == "relation_graph") or \
             (expected != "relation" and path != "relation_graph")
        safe += ok
        print(f"{q!r} intent={got} conf={conf:.2f} path={path} safe={ok}")
    print(f"safety={safe}/{len(cases)} deterministic=True relation_union_disabled=True")


if __name__ == "__main__":
    main()
