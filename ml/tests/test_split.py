import numpy as np
import pytest

from verabank_ml.split import assign_partition, validate_split

SPLIT = {"train": [1, 400], "valid": [425, 550], "test": [575, 744], "gap_steps": 24}


def test_assign_partition_boundaries():
    steps = np.array([0, 1, 400, 401, 424, 425, 550, 551, 574, 575, 744, 745])
    expected = [
        "gap",
        "train",
        "train",
        "gap",
        "gap",
        "valid",
        "valid",
        "gap",
        "gap",
        "test",
        "test",
        "gap",
    ]
    assert assign_partition(steps, SPLIT).tolist() == expected


def test_real_config_split_is_consistent():
    from verabank_ml.features import load_config

    validate_split(load_config()["split"])


def test_validate_split_rejects_wrong_gap():
    with pytest.raises(ValueError, match="hueco"):
        validate_split({**SPLIT, "valid": [430, 550]})
