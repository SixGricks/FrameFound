from framefound.search.ranking import RRF_K, fuse


def test_single_list_preserves_order() -> None:
    hits = fuse({"visual": ["a", "b", "c"]})
    assert [h.key for h in hits] == ["a", "b", "c"]


def test_agreement_across_strategies_wins() -> None:
    # "b" is second in both lists; "a" and "c" each top one list only.
    hits = fuse({"visual": ["a", "b"], "transcript": ["c", "b"]})
    assert hits[0].key == "b"
    assert hits[0].reasons == ["visual", "transcript"]


def test_reasons_record_every_matching_strategy() -> None:
    hits = {h.key: h for h in fuse({"visual": ["x"], "filename": ["x"], "transcript": ["y"]})}
    assert set(hits["x"].reasons) == {"visual", "filename"}
    assert hits["y"].reasons == ["transcript"]


def test_weights_shift_priority() -> None:
    lists = {"visual": ["a"], "transcript": ["b"]}
    assert fuse(lists, weights={"transcript": 5.0})[0].key == "b"
    assert fuse(lists, weights={"visual": 5.0})[0].key == "a"


def test_score_uses_rrf_formula() -> None:
    hit = fuse({"visual": ["only"]})[0]
    assert hit.score == 1 / (RRF_K + 1)


def test_empty_input_is_empty_output() -> None:
    assert fuse({}) == []
    assert fuse({"visual": []}) == []


def test_ties_break_deterministically() -> None:
    # Equal scores must not reorder between calls.
    first = [h.key for h in fuse({"visual": ["b", "a"], "filename": ["a", "b"]})]
    second = [h.key for h in fuse({"visual": ["b", "a"], "filename": ["a", "b"]})]
    assert first == second
