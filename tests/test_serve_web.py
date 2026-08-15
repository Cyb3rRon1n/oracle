from __future__ import annotations

from unittest.mock import patch

import pytest

# textual-serve is an optional dependency (pyproject.toml's "web" extra,
# ROADMAP.md 2026-08-15) - a plain dev install (pip install -e ".[dev]")
# never has it, so this whole file gracefully skips rather than failing
# collection, the same "optional extra, optional test coverage" shape
# this project's Ollama-only tests would need if it had any (it doesn't -
# OllamaNarrator's own tests mock the ollama client instead of skipping).
textual_serve = pytest.importorskip("textual_serve")


def test_main_launches_client_main_as_the_served_command():
    from client.serve_web import main

    with patch("client.serve_web.Server") as mock_server_cls:
        main()

        mock_server_cls.assert_called_once()
        args, kwargs = mock_server_cls.call_args
        assert args[0] == "python -m client.main"
        mock_server_cls.return_value.serve.assert_called_once()


def test_main_reads_host_port_and_public_url_from_env():
    from client.serve_web import main

    env = {"WEB_HOST": "0.0.0.0", "WEB_PORT": "9001", "WEB_PUBLIC_URL": "https://oracle.example.com"}
    with patch("client.serve_web.Server") as mock_server_cls, patch("os.environ.get", side_effect=lambda k, d=None: env.get(k, d)):
        main()

        _, kwargs = mock_server_cls.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 9001
        assert kwargs["public_url"] == "https://oracle.example.com"


def test_main_defaults_to_localhost_8000_with_no_public_url():
    from client.serve_web import main

    with patch("client.serve_web.Server") as mock_server_cls, patch.dict("os.environ", {}, clear=True):
        main()

        _, kwargs = mock_server_cls.call_args
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 8000
        assert kwargs["public_url"] is None
