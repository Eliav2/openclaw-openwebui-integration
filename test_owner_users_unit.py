"""Owner users (OWNER_USERS valve): a user not on the list reaches the Gateway
over a second connection without operator.admin, so the Gateway does not
treat that user's turns as the owner's and withholds owner-only tools.

Run: python3 -m unittest test_owner_users_unit
"""
import ast
import glob
import os
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)

import openclaw_pipe
from openclaw_pipe import Pipe, GATEWAY_SCOPES, _sign_challenge

HERE = os.path.dirname(os.path.abspath(__file__))
RESTRICTED = ["operator.read", "operator.write"]
# Every request that starts, steers or ends a run. Sent on the admin
# connection, such a request makes the turn the owner's.
RUN_METHODS = {"chat.send", "chat.abort", "sessions.patch"}


class OwnerUsersValveTests(unittest.TestCase):
    def test_the_valve_exists_and_is_empty_by_default(self):
        self.assertEqual(Pipe.Valves().OWNER_USERS, "")

    def test_parse_ids_and_emails(self):
        owners = openclaw_pipe._parse_owner_users(" U-1 , Someone@Example.org,,u-2 ")
        self.assertEqual(owners, frozenset({"u-1", "u-2", "someone@example.org"}))

    def test_empty_means_everyone_is_an_owner(self):
        owners = openclaw_pipe._parse_owner_users("")
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "anyone", "x@y.z"))

    def test_match_by_id_or_email_case_insensitively(self):
        owners = openclaw_pipe._parse_owner_users("u-1, owner@example.org")
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "U-1", None))
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "u-9", "Owner@Example.org"))
        self.assertFalse(openclaw_pipe._is_owner_user(owners, "u-9", "guest@example.org"))
        self.assertFalse(openclaw_pipe._is_owner_user(owners, None, None))


class SignedScopesTests(unittest.TestCase):
    """The Gateway checks the scope list inside the signed device payload, so
    a connection must sign exactly the scopes it requests."""

    IDENT = {"id": "dev-1", "publicKey": "pub", "privateKey": "pem"}

    def _signed_payload(self, **kw):
        seen = []

        class Key:
            def sign(self, data):
                seen.append(data.decode())
                return b"sig"
        with mock.patch.object(openclaw_pipe.serialization, "load_pem_private_key",
                               lambda *a, **k: Key()):
            _sign_challenge(self.IDENT, "n1", 7, token_str="tok", **kw)
        return seen[0]

    def _expected(self, scopes):
        return "|".join(["v2", "dev-1", "webchat", "cli", "operator", ",".join(scopes), "7", "tok", "n1"])

    def test_default_signs_todays_scopes(self):
        self.assertEqual(self._signed_payload(), self._expected(GATEWAY_SCOPES))

    def test_restricted_signs_read_write_only(self):
        self.assertEqual(self._signed_payload(scopes=RESTRICTED), self._expected(RESTRICTED))


class NoRunRequestBypassesTheRouterTests(unittest.TestCase):
    """A run request sent on the admin connection for a non-owner chat would
    make that turn the owner's. Every such request goes through
    `_run_request`, which picks the connection; none is sent anywhere else."""

    def test_run_methods_appear_only_inside_the_router(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(HERE, "src", "openclaw_pipe_pkg", "*.py"))):
            tree = ast.parse(open(path).read())
            router = [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_run_request"]
            inside = {id(x) for r in router for x in ast.walk(r)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and node.value in RUN_METHODS
                        and id(node) not in inside):
                    offenders.append(f"{os.path.basename(path)}:{node.lineno} {node.value}")
        self.assertEqual(offenders, [], "run requests that bypass _run_request")


if __name__ == "__main__":
    unittest.main()
