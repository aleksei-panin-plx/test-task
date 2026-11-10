from cardforge.evaluate.pairwise_style import PairAnswer, anchors, candidate_score, comparisons


def answer(overall: str) -> PairAnswer:
    return PairAnswer(object=overall, material=overall, environment=overall,
                      overall=overall, confidence=3, note="x")


def test_candidate_score_follows_presentation_order():
    assert candidate_score(answer("A"), "candidate-first") == 1.0
    assert candidate_score(answer("A"), "anchor-first") == 0.0
    assert candidate_score(answer("B"), "anchor-first") == 1.0
    assert candidate_score(answer("equal"), "candidate-first") == 0.5


def test_anchors_prefer_same_category_and_both_orders():
    item = {"spec": {"category": 4}, "reference_context": [
        {"role": "style", "category": 1, "image": "ref-1.png"},
        {"role": "material", "category": 4, "image": "ref-3.png"},
        {"role": "category", "category": 4, "image": "ref-2.png"},
    ]}
    assert anchors(item) == ["ref-2.png", "ref-3.png"]
    assert len(comparisons(item)) == 4
