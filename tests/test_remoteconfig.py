"""Unit tests for Remote Config (openfirebase.remoteconfig)."""

import pytest

from openfirebase.remoteconfig import RemoteConfig, RemoteConfigError
from openfirebase.storage import MemoryStore


class TestRemoteConfig:
    def setup_method(self):
        self.rc = RemoteConfig(MemoryStore())

    # ---- parameters ---------------------------------------------------------

    def test_set_and_get_parameter(self):
        self.rc.set_parameter("greeting", "Hello")
        param = self.rc.get_parameter("greeting")
        assert param is not None
        assert param["key"] == "greeting"
        assert param["default_value"] == "Hello"

    def test_list_parameters(self):
        self.rc.set_parameter("a", "1")
        self.rc.set_parameter("b", "2")
        params = self.rc.list_parameters()
        keys = {p["key"] for p in params}
        assert keys == {"a", "b"}

    def test_delete_parameter(self):
        self.rc.set_parameter("temp", "x")
        assert self.rc.get_parameter("temp") is not None
        ok = self.rc.delete_parameter("temp")
        assert ok is True
        assert self.rc.get_parameter("temp") is None

    def test_delete_nonexistent(self):
        assert self.rc.delete_parameter("nope") is False

    def test_default_value_is_string(self):
        self.rc.set_parameter("count", "42")
        param = self.rc.get_parameter("count")
        assert param["default_value"] == "42"
        assert isinstance(param["default_value"], str)

    def test_int_default_converted_to_str(self):
        self.rc.set_parameter("n", 99)  # type: ignore[arg-type]
        param = self.rc.get_parameter("n")
        assert param["default_value"] == "99"

    # ---- conditions ---------------------------------------------------------

    def test_set_and_get_condition(self):
        self.rc.set_condition("ios_only", [
            {"field": "platform", "op": "==", "value": "ios"}
        ])
        cond = self.rc.get_condition("ios_only")
        assert cond is not None
        assert cond["name"] == "ios_only"

    def test_list_conditions(self):
        self.rc.set_condition("cond1", [])
        self.rc.set_condition("cond2", [])
        conds = self.rc.list_conditions()
        assert len(conds) >= 2

    def test_delete_condition(self):
        self.rc.set_condition("tmp", [])
        ok = self.rc.delete_condition("tmp")
        assert ok is True
        assert self.rc.get_condition("tmp") is None

    # ---- fetch / evaluate ---------------------------------------------------

    def test_fetch_no_params(self):
        result = self.rc.fetch()
        assert isinstance(result, dict)
        assert len(result) == 0

    def test_fetch_default_value(self):
        self.rc.set_parameter("bg_color", "white")
        config = self.rc.fetch()
        assert config["bg_color"] == "white"

    def test_fetch_with_matching_condition(self):
        self.rc.set_condition("android", [
            {"field": "platform", "op": "==", "value": "android"}
        ])
        self.rc.set_parameter("theme", "light", conditional_values=[
            {"condition": "android", "value": "dark"}
        ])
        # android client
        config = self.rc.fetch({"platform": "android"})
        assert config["theme"] == "dark"

    def test_fetch_with_non_matching_condition(self):
        self.rc.set_condition("ios", [
            {"field": "platform", "op": "==", "value": "ios"}
        ])
        self.rc.set_parameter("theme", "light", conditional_values=[
            {"condition": "ios", "value": "cupertino"}
        ])
        config = self.rc.fetch({"platform": "android"})
        assert config["theme"] == "light"

    def test_evaluate_single_key(self):
        self.rc.set_parameter("flag", "off")
        assert self.rc.evaluate("flag") == "off"

    def test_evaluate_nonexistent_key(self):
        assert self.rc.evaluate("does_not_exist") is None

    def test_evaluate_with_condition_match(self):
        self.rc.set_condition("beta", [
            {"field": "user_group", "op": "==", "value": "beta"}
        ])
        self.rc.set_parameter("feature_x", "false", conditional_values=[
            {"condition": "beta", "value": "true"}
        ])
        assert self.rc.evaluate("feature_x", {"user_group": "beta"}) == "true"
        assert self.rc.evaluate("feature_x", {"user_group": "stable"}) == "false"

    def test_condition_op_contains(self):
        self.rc.set_condition("version_2x", [
            {"field": "app_version", "op": "contains", "value": "2."}
        ])
        self.rc.set_parameter("msg", "old", conditional_values=[
            {"condition": "version_2x", "value": "new"}
        ])
        assert self.rc.evaluate("msg", {"app_version": "2.3.1"}) == "new"
        assert self.rc.evaluate("msg", {"app_version": "1.9.0"}) == "old"

    def test_condition_op_startswith(self):
        self.rc.set_condition("v3", [
            {"field": "app_version", "op": "startsWith", "value": "3."}
        ])
        self.rc.set_parameter("x", "a", conditional_values=[
            {"condition": "v3", "value": "b"}
        ])
        assert self.rc.evaluate("x", {"app_version": "3.0.0"}) == "b"
        assert self.rc.evaluate("x", {"app_version": "2.0.0"}) == "a"

    def test_condition_op_ne(self):
        self.rc.set_condition("not_ios", [
            {"field": "platform", "op": "!=", "value": "ios"}
        ])
        self.rc.set_parameter("y", "a", conditional_values=[
            {"condition": "not_ios", "value": "b"}
        ])
        assert self.rc.evaluate("y", {"platform": "android"}) == "b"
        assert self.rc.evaluate("y", {"platform": "ios"}) == "a"

    def test_empty_condition_always_true(self):
        self.rc.set_condition("always", [])   # empty = always matches
        self.rc.set_parameter("z", "default", conditional_values=[
            {"condition": "always", "value": "override"}
        ])
        assert self.rc.evaluate("z", {}) == "override"

    # ---- version / template -------------------------------------------------

    def test_version_increments_on_change(self):
        v0 = self.rc.get_version()
        self.rc.set_parameter("k", "v")
        assert self.rc.get_version() == v0 + 1

    def test_get_template(self):
        self.rc.set_parameter("p1", "val1")
        self.rc.set_condition("c1", [])
        tmpl = self.rc.get_template()
        assert "version" in tmpl
        assert "parameters" in tmpl
        assert "conditions" in tmpl
        assert "p1" in tmpl["parameters"]

    def test_multiple_conditions_first_wins(self):
        self.rc.set_condition("c1", [{"field": "x", "op": "==", "value": "a"}])
        self.rc.set_condition("c2", [{"field": "x", "op": "==", "value": "a"}])
        self.rc.set_parameter("q", "default", conditional_values=[
            {"condition": "c1", "value": "first"},
            {"condition": "c2", "value": "second"},
        ])
        assert self.rc.evaluate("q", {"x": "a"}) == "first"
