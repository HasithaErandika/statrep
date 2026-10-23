import pytest

from app import config


def test_recipients_split_validate_and_dedupe():
    assert config._recipients("a@x.com; b@y.org\n A@X.com, nope, c@z.co") == ["a@x.com", "b@y.org", "c@z.co"]


def test_times_are_validated_and_normalised():
    assert config._times("7:45, 06:00,20:00,06:00") == ["06:00", "07:45", "20:00"]
    with pytest.raises(config.ConfigError):
        config._times("7.45")
    with pytest.raises(config.ConfigError):
        config._times("24:00")


def test_int_settings_give_clear_errors(monkeypatch):
    monkeypatch.setenv("CHECK_TIMEOUT", "20s")
    with pytest.raises(config.ConfigError, match="CHECK_TIMEOUT"):
        config._int("CHECK_TIMEOUT", 20, 1)
    monkeypatch.setenv("CHECK_TIMEOUT", "0")
    with pytest.raises(config.ConfigError, match="at least 1"):
        config._int("CHECK_TIMEOUT", 20, 1)
    monkeypatch.delenv("CHECK_TIMEOUT")
    assert config._int("CHECK_TIMEOUT", 20, 1) == 20


@pytest.mark.parametrize("raw, expected", [
    ("https://a.com/", {"url": "https://a.com/", "name": None}),
    ({"url": "b.org", "name": "B"}, {"url": "https://b.org", "name": "B"}),
])
def test_site_entries(raw, expected):
    assert config._site_entry(raw) == expected


@pytest.mark.parametrize("raw, error", [
    ({"url": "ftp://x"}, "http(s)"),
    ({"url": "https://x.com", "colour": "red"}, "unknown option: colour"),
    (42, "must be a URL"),
])
def test_bad_site_entries_become_config_errors(raw, error):
    assert error in config._site_entry(raw)["config_error"]


def test_load_sites_yaml_and_errors(tmp_path, monkeypatch):
    f = tmp_path / "sites.yml"
    f.write_text("- https://a.com\n- url: b.org\n  name: B\n")
    monkeypatch.setattr(config, "SITES_FILE", str(f))
    assert [s["url"] for s in config.load_sites()] == ["https://a.com", "https://b.org"]
    f.write_text("- url: [broken\n")
    assert "invalid YAML (line 2)" in config.load_sites()[0]["config_error"]
    f.write_text("url: https://a.com\n")
    assert "must be a list" in config.load_sites()[0]["config_error"]


def test_load_sites_falls_back_to_legacy_txt(tmp_path, monkeypatch):
    txt = tmp_path / "sites.txt"
    txt.write_text("https://a.com\n# comment\n\nb.org\n")
    monkeypatch.setattr(config, "SITES_FILE", str(tmp_path / "missing.yml"))
    monkeypatch.setattr(config, "LEGACY_SITES_FILE", txt)
    assert [s["url"] for s in config.load_sites()] == ["https://a.com", "https://b.org"]


def test_api_entry_fills_secrets_from_env(monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    api = config._api_entry(1, {"name": "A", "url": "https://x.com/?k=${TOKEN}", "method": "post",
                                "headers": {"Authorization": "Bearer ${TOKEN}"}, "body": {"p": "${TOKEN}"},
                                "expect_status": 201})
    assert api["url"] == "https://x.com/?k=s3cret"
    assert api["display_url"] == "https://x.com/?k=${TOKEN}"   # what is shown never has the secret
    assert api["headers"] == {"Authorization": "Bearer s3cret"}
    assert api["body"] == {"p": "s3cret"}
    assert api["expect_status"] == [201]
    assert api["retry"] is False                                # POST is not retried by default


@pytest.mark.parametrize("raw, error", [
    ({"name": "A", "url": "https://x.com", "headers": {"K": "${NOT_SET_ANYWHERE}"}}, "missing in .env: NOT_SET_ANYWHERE"),
    ({"name": "A", "url": "https://x.com", "method": "FETCH"}, "Unsupported method FETCH"),
    ({"name": "A", "url": "x.com"}, "must start with http"),
    ({"name": "A", "url": "https://x.com", "expect_status": "200"}, "expect_status"),
    ({"name": "A", "url": "https://x.com", "typo": 1}, "Unknown option: typo"),
])
def test_bad_api_entries_become_config_errors(raw, error):
    assert error in config._api_entry(1, raw)["config_error"]
