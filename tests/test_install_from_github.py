import io
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from ansible.module_utils import basic

from plugins.modules import install_from_github


class ReleaseFetchReached(Exception):
    pass


@contextmanager
def module_arguments(move_rule):
    arguments = {
        "ANSIBLE_MODULE_ARGS": {
            "repo": "example/project",
            "asset_regex": ".*",
            "move_rules": [move_rule],
            "_ansible_no_log": True,
        }
    }
    output = io.StringIO()
    with patch.object(basic, "_ANSIBLE_ARGS", json.dumps(arguments).encode()):
        with patch.object(basic, "_ANSIBLE_PROFILE", "legacy", create=True):
            with patch("sys.stdout", output):
                with patch.object(
                    install_from_github, "get_json_url", side_effect=ReleaseFetchReached
                ) as fetch:
                    yield output, fetch


class MoveRuleValidationTests(unittest.TestCase):
    def test_invalid_types_fail_before_fetching_release(self):
        invalid_values = {
            "src_regex": 123,
            "dst": 123,
            "mode": [],
            "owner": 123,
            "group": 123,
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key):
                rule = {"src_regex": ".*", "dst": "/tmp/install-target", key: value}
                with module_arguments(rule) as (output, fetch):
                    with self.assertRaises(SystemExit) as raised:
                        install_from_github.main()

                    self.assertEqual(raised.exception.code, 1)
                    result = json.loads(output.getvalue())
                    self.assertTrue(result["failed"])
                    self.assertEqual(
                        result["msg"],
                        f"Some move rule has invalid type for argument '{key}'.",
                    )
                    fetch.assert_not_called()

    def test_valid_types_reach_release_fetch(self):
        for attributes in (
            {},
            {"mode": "0755"},
            {"mode": 0o755, "owner": "root", "group": "root"},
        ):
            with self.subTest(attributes=attributes):
                rule = {"src_regex": ".*", "dst": "/tmp/install-target", **attributes}
                with module_arguments(rule) as (_, fetch):
                    with self.assertRaises(ReleaseFetchReached):
                        install_from_github.main()
                    fetch.assert_called_once_with(
                        "https://api.github.com/repos/example/project/releases/latest"
                    )

    def test_missing_required_fields_fail_before_fetching_release(self):
        for key in ("src_regex", "dst"):
            with self.subTest(key=key):
                rule = {"src_regex": ".*", "dst": "/tmp/install-target"}
                del rule[key]
                with module_arguments(rule) as (output, fetch):
                    with self.assertRaises(SystemExit):
                        install_from_github.main()
                    result = json.loads(output.getvalue())
                    self.assertEqual(
                        result["msg"],
                        f"Some move rule does not have required argument '{key}'.",
                    )
                    fetch.assert_not_called()
