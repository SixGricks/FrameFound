"""The learned look: saved, loaded, and used by auto-edit for tone while the
model keeps straightening and naming."""

import json
from pathlib import Path

from PIL import Image

from framefound.media import compare, looks
from framefound.media import develop as develop_lib


def _install(data_dir: Path, recipe: dict[str, float], n: int = 12) -> None:
    feats = [
        compare.features(Image.new("RGB", (64, 48), (40 + 12 * i, 90, 140 - 5 * i)))
        for i in range(n)
    ]
    look = compare.LearnedLook.fit(feats, [recipe] * n)
    path = data_dir / looks.LOOK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "features": list(compare.FEATURE_NAMES),
                "mean": look.mean.tolist(),
                "scale": look.scale.tolist(),
                "examples": [
                    {"source": f"s/{i}", "features": [float(x) for x in f], "recipe": recipe}
                    for i, f in enumerate(feats)
                ],
            }
        )
    )


def test_no_look_installed_means_none(tmp_path: Path) -> None:
    assert looks.load(tmp_path) is None
    assert looks.examples(tmp_path) == 0


def test_a_saved_look_loads_and_predicts_its_examples(tmp_path: Path) -> None:
    _install(tmp_path, {"exposure": 0.4, "vibrance": 0.3, "keystone": 0.2})
    look = looks.load(tmp_path)
    assert look is not None and looks.examples(tmp_path) == 12
    predicted = looks.predict(look, Image.new("RGB", (64, 48), (80, 90, 120)))
    assert abs(predicted["exposure"] - 0.4) < 1e-6
    assert "keystone" not in predicted, "geometry is never the look's to set"


def test_a_look_from_a_different_feature_set_is_refused(tmp_path: Path) -> None:
    path = tmp_path / looks.LOOK_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"features": ["old"], "mean": [], "scale": [], "examples": []}))
    assert looks.load(tmp_path) is None


def test_combine_takes_tone_from_the_look_and_straightening_from_the_model() -> None:
    recipe = looks.combine(
        {"exposure": 0.4, "shadows": 0.2},
        {"exposure": 1.5, "rotate": -1.2, "keystone": 0.3, "sky": {"name": "blue.jpg"}},
    )
    assert recipe["exposure"] == 0.4, "the model's tone is not blended back in"
    assert recipe["rotate"] == -1.2 and recipe["keystone"] == 0.3
    assert recipe["sky"] == {"name": "blue.jpg"}
    assert set(develop_lib.clean_recipe(recipe)) >= {"exposure", "shadows", "rotate"}
