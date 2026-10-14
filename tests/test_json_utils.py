import math

import pytest

from src.json_utils import sanitize_for_json


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_becomes_none(value):
    assert sanitize_for_json(value) is None


@pytest.mark.parametrize("value", [0.0, 1.5, -3.25])
def test_finite_float_is_unchanged(value):
    assert sanitize_for_json(value) == value


@pytest.mark.parametrize("value", [None, "text", 42, True])
def test_non_float_types_pass_through_unchanged(value):
    assert sanitize_for_json(value) is value or sanitize_for_json(value) == value


def test_nan_inside_nested_dict_is_cleaned():
    obj = {"price": 10.5, "debt_to_equity": float("nan"), "nested": {"growth": float("inf")}}
    assert sanitize_for_json(obj) == {"price": 10.5, "debt_to_equity": None, "nested": {"growth": None}}


def test_nan_inside_list_is_cleaned():
    assert sanitize_for_json([1.0, float("nan"), 3.0]) == [1.0, None, 3.0]


def test_tuple_is_converted_to_list_and_cleaned():
    assert sanitize_for_json((1.0, float("nan"))) == [1.0, None]
