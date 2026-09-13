"""
test_run_all_env.py
===================
Offline unit tests for the run_all launcher's child-process environment.

Covers ``build_child_env()`` (scripts/run_all.py):

* direct browser -> backend mode by default (``NEXT_PUBLIC_API_URL``) -
  the fix for the Next.js dev proxy's ~30 s ceiling, which drops slow
  ``/analyze`` turns with ``Failed to proxy ... socket hang up``;
* the ``--use-proxy`` opt-out, which restores the old proxied behaviour;
* the automatic ``CORS_ALLOW_ORIGINS`` pre-fill (localhost, 127.0.0.1
  and the LAN IP on the actual frontend port).

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

RUN_ALL_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "run_all.py"
)


def _load_run_all():
    """Import scripts/run_all.py without polluting sys.path."""

    spec = importlib.util.spec_from_file_location(
        "run_all_under_test", RUN_ALL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_all = _load_run_all()


def _make_args(**overrides):
    params = {
        "backend_port": 8001,
        "frontend_port": 3000,
        "use_proxy": False,
    }
    params.update(overrides)
    return SimpleNamespace(**params)


class TestBuildChildEnv(unittest.TestCase):

    def test_direct_mode_sets_public_api_url(self):
        """Default mode: the browser calls FastAPI directly."""

        with patch.dict(os.environ, {}, clear=True):
            env = run_all.build_child_env(_make_args())

        self.assertEqual(
            env["NEXT_PUBLIC_API_URL"], "http://127.0.0.1:8001"
        )
        # The server-side proxy target is always configured as fallback.
        self.assertEqual(
            env["BACKEND_INTERNAL_URL"], "http://127.0.0.1:8001"
        )

    def test_use_proxy_leaves_browser_on_same_origin(self):
        """--use-proxy: no NEXT_PUBLIC_API_URL, browser uses /backend-api."""

        with patch.dict(
            os.environ,
            {"NEXT_PUBLIC_API_URL": "http://stale.example"},
            clear=True,
        ):
            env = run_all.build_child_env(_make_args(use_proxy=True))

        self.assertNotIn("NEXT_PUBLIC_API_URL", env)
        self.assertEqual(
            env["BACKEND_INTERNAL_URL"], "http://127.0.0.1:8001"
        )

    def test_cors_prefilled_with_dashboard_origins(self):
        """Direct mode is cross-origin, so CORS origins are pre-filled."""

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                run_all, "local_lan_ip", return_value=None
            ):
                env = run_all.build_child_env(_make_args())

        origins = env["CORS_ALLOW_ORIGINS"].split(",")

        self.assertIn("http://localhost:3000", origins)
        self.assertIn("http://127.0.0.1:3000", origins)

    def test_cors_includes_lan_ip_when_detected(self):
        """Opening the dashboard from another device must keep working."""

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                run_all, "local_lan_ip", return_value="192.168.1.10"
            ):
                env = run_all.build_child_env(_make_args())

        self.assertIn(
            "http://192.168.1.10:3000",
            env["CORS_ALLOW_ORIGINS"].split(","),
        )

    def test_custom_ports_are_honoured(self):
        """Non-default ports must appear in both URLs and CORS origins."""

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                run_all, "local_lan_ip", return_value=None
            ):
                env = run_all.build_child_env(
                    _make_args(backend_port=9000, frontend_port=4000)
                )

        self.assertEqual(
            env["NEXT_PUBLIC_API_URL"], "http://127.0.0.1:9000"
        )
        self.assertEqual(
            env["BACKEND_INTERNAL_URL"], "http://127.0.0.1:9000"
        )

        origins = env["CORS_ALLOW_ORIGINS"].split(",")
        self.assertIn("http://localhost:4000", origins)
        self.assertIn("http://127.0.0.1:4000", origins)

    def test_explicit_cors_value_is_respected(self):
        """An operator-set CORS_ALLOW_ORIGINS is never overwritten."""

        with patch.dict(
            os.environ,
            {"CORS_ALLOW_ORIGINS": "https://dashboard.example"},
            clear=True,
        ):
            env = run_all.build_child_env(_make_args())

        self.assertEqual(
            env["CORS_ALLOW_ORIGINS"], "https://dashboard.example"
        )


class TestLocalLanIp(unittest.TestCase):

    def test_returns_none_or_a_non_loopback_ipv4(self):
        """Environment-dependent, but the contract always holds."""

        ip = run_all.local_lan_ip()

        if ip is None:
            return

        parts = ip.split(".")

        self.assertEqual(len(parts), 4)
        self.assertTrue(all(part.isdigit() for part in parts))
        self.assertFalse(ip.startswith("127."))


if __name__ == "__main__":
    unittest.main()
