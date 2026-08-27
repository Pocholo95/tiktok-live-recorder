import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import utils.utils as utils_module  # noqa: E402


def test_settings_page_shows_masked_status_when_configured(client, monkeypatch):
    monkeypatch.setattr(
        utils_module, "read_cookies", lambda: {"sessionid_ss": "abcdef1234"}
    )
    monkeypatch.setattr(
        utils_module, "read_telegram_config", lambda: {"api_id": "999999"}
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "1234" in response.text
    assert "abcdef1234" not in response.text  # full secret never rendered
    assert "no configurado" in response.text  # api_hash still unset


def test_update_cookies_merges_and_preserves_unset_fields(client, monkeypatch):
    store = {"sessionid_ss": "old_value", "tt-target-idc": "useast2a"}
    monkeypatch.setattr(utils_module, "read_cookies", lambda: dict(store))

    written = {}
    monkeypatch.setattr(utils_module, "write_cookies", written.update)

    response = client.post(
        "/settings/cookies",
        data={"sessionid_ss": "new_value", "tt_target_idc": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=cookies"
    assert written["sessionid_ss"] == "new_value"
    assert written["tt-target-idc"] == "useast2a"  # left untouched


def test_update_cookies_blank_fields_do_not_overwrite(client, monkeypatch):
    store = {"sessionid_ss": "keep_me"}
    monkeypatch.setattr(utils_module, "read_cookies", lambda: dict(store))

    written = {}
    monkeypatch.setattr(utils_module, "write_cookies", written.update)

    client.post(
        "/settings/cookies",
        data={"sessionid_ss": "", "tt_target_idc": ""},
        follow_redirects=False,
    )

    assert written["sessionid_ss"] == "keep_me"
    assert "tt-target-idc" not in written


def test_update_cookies_shows_friendly_error_instead_of_500(client, monkeypatch):
    monkeypatch.setattr(utils_module, "read_cookies", lambda: {})

    def _raise_readonly(data):
        raise OSError("Read-only file system")

    monkeypatch.setattr(utils_module, "write_cookies", _raise_readonly)

    response = client.post(
        "/settings/cookies",
        data={"sessionid_ss": "new_value", "tt_target_idc": ""},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "No se pudo guardar cookies.json" in response.text
    assert "solo lectura" in response.text


def test_update_telegram_shows_friendly_error_instead_of_500(client, monkeypatch):
    monkeypatch.setattr(utils_module, "read_telegram_config", lambda: {})

    def _raise_readonly(data):
        raise OSError("Read-only file system")

    monkeypatch.setattr(utils_module, "write_telegram_config", _raise_readonly)

    response = client.post(
        "/settings/telegram",
        data={"api_id": "123", "api_hash": "", "chat_id": ""},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "No se pudo guardar telegram.json" in response.text


def test_update_telegram_merges_fields(client, monkeypatch):
    monkeypatch.setattr(utils_module, "read_telegram_config", lambda: {})

    written = {}
    monkeypatch.setattr(utils_module, "write_telegram_config", written.update)

    response = client.post(
        "/settings/telegram",
        data={"api_id": "123", "api_hash": "hash", "chat_id": "-100987"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=telegram"
    assert written["api_id"] == 123
    assert written["api_hash"] == "hash"
    assert written["chat_id"] == -100987
