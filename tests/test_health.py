from backoffice_agents.config import Settings
from backoffice_agents.health import run_doctor


def test_doctor_passes_on_default_mock_settings():
    checks = {c.name: c for c in run_doctor(Settings(_env_file=None))}
    assert checks["JEV_MODE"].ok and checks["JEV_MODE"].detail == "emulated"
    assert checks["TYPESAFE_API_KEY"].ok
    assert checks["IMAP"].ok
    assert checks["TELEGRAM"].ok


def test_doctor_fails_when_real_jev_has_no_key():
    settings = Settings(_env_file=None, jev_mode="real", typesafe_api_key=None)
    checks = {c.name: c for c in run_doctor(settings)}
    assert checks["TYPESAFE_API_KEY"].ok is False


def test_doctor_fails_when_imap_is_chosen_without_credentials():
    settings = Settings(_env_file=None, email_adapter="imap")
    checks = {c.name: c for c in run_doctor(settings)}
    assert checks["IMAP"].ok is False
    assert "faltam" in checks["IMAP"].detail


def test_doctor_fails_when_imap_login_fails(monkeypatch):
    settings = Settings(_env_file=None, email_adapter="imap",
                        imap_user="u", imap_password="p", smtp_user="u", smtp_password="p")

    class Boom:
        def fetch_unread(self):
            raise ConnectionError("down")

    monkeypatch.setattr("backoffice_agents.health.build_adapters",
                        lambda _settings: type("A", (), {"email": Boom()})())
    checks = {c.name: c for c in run_doctor(settings)}
    assert checks["IMAP"].ok is False
    assert "falhou" in checks["IMAP"].detail
