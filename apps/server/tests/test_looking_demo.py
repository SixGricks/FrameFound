"""The demo's looks (looking/demo.py; design L2 D23): made up, but shaped like looks.

A demo comparison stores these in place of the demo model server's random
JSON, so they must be valid answers to the real question, use only the words
the model was shown, and be the same every time the same item is asked.
"""

from framefound.looking import demo
from framefound.looking.prompt import LookAnswer

VOCABULARY = ["drone", "golf course", "exterior", "Kitchen", "sunset", "barn", "pond"]


def _items(n: int = 40) -> list[str]:
    return [f"mlx-community/demo-small-4bit:item-{i}" for i in range(n)]


def test_the_demo_look_uses_only_words_from_the_vocabulary_it_was_given() -> None:
    for seed in _items():
        look = demo.answer_for(seed, VOCABULARY, shot=True)
        assert 1 <= len(look.matches) <= demo.MOST_TAGS
        assert {guess.tag for guess in look.matches} <= set(VOCABULARY)
        # A demo model has seen nothing it could name.
        assert look.new_tags == []
        assert len({guess.tag for guess in look.matches}) == len(look.matches)

    # Nothing to draw from: no tags at all, rather than a word of its own.
    assert demo.answer_for("x", [], shot=False).matches == []
    # A word too long for a model to write is never drawn, cut or whole; the
    # same word twice, or spaced oddly, is one word as the model was shown it.
    long_word = "a" * 61
    for seed in _items(20):
        look = demo.answer_for(seed, [long_word, "  barn ", "barn", "BARN"], shot=True)
        assert [guess.tag for guess in look.matches] == ["barn"]


def test_the_same_seed_gives_the_same_demo_look() -> None:
    for seed in _items(10):
        assert demo.answer_for(seed, VOCABULARY, shot=True) == demo.answer_for(
            seed, list(VOCABULARY), shot=True
        )
        assert demo.figures_for(seed) == demo.figures_for(seed)


def test_two_demo_models_answer_the_same_item_differently() -> None:
    small = [
        demo.answer_for(f"mlx-community/demo-small-4bit:{i}", VOCABULARY, shot=True)
        for i in range(30)
    ]
    large = [
        demo.answer_for(f"mlx-community/demo-large-4bit:{i}", VOCABULARY, shot=True)
        for i in range(30)
    ]
    assert sum(a != b for a, b in zip(small, large, strict=True)) > 25
    assert [a.score for a in small] != [b.score for b in large]


def test_a_demo_look_is_an_answer_in_the_form_asked_for() -> None:
    for i, seed in enumerate(_items()):
        shot = i % 2 == 0
        look = demo.answer_for(seed, VOCABULARY, shot=shot)
        # It passes the real schema, as JSON, exactly as a model's answer must.
        assert LookAnswer.model_validate_json(look.model_dump_json()) == look
        assert demo.SCORE_LOW <= look.score <= demo.SCORE_HIGH
        assert look.caption in (demo.SHOT_CAPTIONS if shot else demo.PHOTO_CAPTIONS)
        # It says, on every answer, that nobody looked.
        assert look.reason == demo.REASON
        confidences = [guess.confidence for guess in look.matches]
        assert confidences == sorted(confidences, reverse=True)
        assert all(demo.CONFIDENCE_LOW <= c <= demo.CONFIDENCE_HIGH for c in confidences)


def test_demo_figures_spread_enough_to_have_something_to_count() -> None:
    looks = [demo.answer_for(seed, VOCABULARY, shot=True) for seed in _items()]
    # Different scores, and confidences on both sides of each floor Compare
    # counts at, so a demo comparison's figures are not all one number.
    assert len({look.score for look in looks}) > 10
    confidences = [guess.confidence for look in looks for guess in look.matches]
    assert any(c < 0.6 for c in confidences)
    assert any(0.6 <= c < 0.85 for c in confidences)
    assert any(c >= 0.85 for c in confidences)

    figures = [demo.figures_for(seed) for seed in _items()]
    seconds = {s for s, _gb in figures}
    memory = {gb for _s, gb in figures}
    assert len(seconds) > 10 and len(memory) > 10
    assert all(demo.SECONDS[0] <= s <= demo.SECONDS[1] for s in seconds)
    assert all(demo.MEMORY_GB[0] <= gb <= demo.MEMORY_GB[1] for gb in memory)
