"""GrowLead patch: write ticket gate (gads/interventions.py + cli._dispatch)."""
from __future__ import annotations

import json
import re

import pytest
import requests

from gads import cli, interventions
from tests.conftest import ns

WHY = "test: dostatečně dlouhý důvod zásahu"
TICKET = "int_abc123"


class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Http:
    """Záznam HTTP volání; monkeypatchuje se místo gads.interventions.requests."""

    RequestException = requests.RequestException
    HTTPError = requests.HTTPError

    def __init__(self, get_resp=None, post_resp=None, get_exc=None):
        self.get_resp, self.post_resp, self.get_exc = get_resp, post_resp, get_exc
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.gets.append((url, headers or {}))
        if self.get_exc:
            raise self.get_exc
        return self.get_resp

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json or {}))
        return self.post_resp or _Resp(200, {})


def _claimed(external="1234567890", status="claimed", envelope=False):
    row = {"id": TICKET, "status": status}
    account = {"externalAccountId": external}
    if envelope:  # GOV-1 REST shape: adAccount vedle intervention
        return _Resp(200, {"intervention": row, "adAccount": account})
    return _Resp(200, {**row, "adAccount": account})


@pytest.fixture
def strict_env(monkeypatch):
    monkeypatch.setenv("GL_ADS_URL", "https://gl-ads.test/")
    monkeypatch.setenv("GL_ADS_API_KEY", "key_test")
    monkeypatch.setenv("GL_ADS_AGENT", "claude-code/test")
    monkeypatch.delenv("GL_ADS_TICKET_GATE", raising=False)


def _confirmed(**kw):
    base = {"command": "campaign-status", "confirm": True, "ticket": TICKET, "why": WHY}
    base.update(kw)
    return ns(**base)


# ---------------------------------------------------------------------------
# dry-run passes through untouched
# ---------------------------------------------------------------------------
def test_dry_run_skips_gate_entirely(monkeypatch):
    http = _Http()
    monkeypatch.setattr(interventions, "requests", http)
    calls = []
    args = ns(command="campaign-status", confirm=False, func=lambda a: calls.append(a))
    cli._dispatch(args)
    assert calls == [args]
    assert http.gets == [] and http.posts == []


# ---------------------------------------------------------------------------
# strict gate refusals — nothing runs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"ticket": None},
    {"why": None},
    {"why": "krátké"},
])
def test_strict_refuses_missing_ticket_or_why(monkeypatch, strict_env, kw):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    ran = []
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: ran.append(a), **kw))
    assert exc.value.code == 2
    assert ran == [] and http.gets == [] and http.posts == []


def test_strict_refuses_missing_env(monkeypatch, strict_env):
    monkeypatch.delenv("GL_ADS_API_KEY")
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2 and http.gets == []


@pytest.mark.parametrize("resp, code", [
    (_claimed(status="approved"), 2),
    (_claimed(external="9999999999"), 2),
    (_Resp(404, {}), 2),
    (_Resp(403, {}), 2),
    (_Resp(503, {}), 3),
])
def test_strict_preflight_rejects_bad_ticket(monkeypatch, strict_env, resp, code):
    http = _Http(get_resp=resp)
    monkeypatch.setattr(interventions, "requests", http)
    ran = []
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: ran.append(a)))
    assert exc.value.code == code
    assert ran == [] and http.posts == []
    assert http.gets[0][0] == f"https://gl-ads.test/api/v1/interventions/{TICKET}"
    assert http.gets[0][1]["Authorization"] == "Bearer key_test"


def test_strict_network_error_exits_3(monkeypatch, strict_env):
    http = _Http(get_exc=requests.ConnectionError("down"))
    monkeypatch.setattr(interventions, "requests", http)
    ran = []
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: ran.append(a)))
    assert exc.value.code == 3 and ran == []


# ---------------------------------------------------------------------------
# strict happy path — GET, command, POST with collected resource names
# ---------------------------------------------------------------------------
def test_strict_happy_path_posts_mark_executed(monkeypatch, strict_env):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)

    class _R:
        def __init__(self, rn):
            self.resource_name = rn

    class _Response:
        results = [_R("customers/1234567890/campaigns/1"), _R("customers/1234567890/campaigns/2")]

    def fake_cmd(args):
        interventions.record_result(_Response())

    cli._dispatch(_confirmed(func=fake_cmd))
    assert len(http.gets) == 1 and len(http.posts) == 1
    url, body = http.posts[0]
    assert url == f"https://gl-ads.test/api/v1/interventions/{TICKET}/executed"
    assert body["externalChangeIds"] == ["customers/1234567890/campaigns/1",
                                         "customers/1234567890/campaigns/2"]
    assert body["after"]["ok"] is True
    assert body["after"]["command"] == "campaign-status"
    assert body["after"]["agent"] == "claude-code/test"


def test_strict_postflight_reports_failure_and_reraises(monkeypatch, strict_env):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)

    def boom(args):
        raise RuntimeError("api exploded")

    with pytest.raises(RuntimeError):
        cli._dispatch(_confirmed(func=boom))
    assert http.posts[0][1]["after"]["ok"] is False


def test_postflight_failure_never_raises(monkeypatch, strict_env, capsys):
    http = _Http(get_resp=_claimed(), post_resp=_Resp(500, {}))
    monkeypatch.setattr(interventions, "requests", http)
    cli._dispatch(_confirmed(func=lambda a: None))
    err = capsys.readouterr().err
    assert "mark-executed" in err and TICKET in err


def test_preflight_accepts_gov1_envelope_shape(monkeypatch, strict_env):
    http = _Http(get_resp=_claimed(envelope=True))
    monkeypatch.setattr(interventions, "requests", http)
    ctx = interventions.preflight(_confirmed())
    assert ctx is not None and ctx.ticket == TICKET


def test_preflight_envelope_with_wrong_status_is_refused(monkeypatch, strict_env):
    http = _Http(get_resp=_claimed(status="executed", envelope=True))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2


def test_customer_id_dashes_are_normalised(monkeypatch, strict_env):
    http = _Http(get_resp=_claimed(external="123-456-7890"))
    monkeypatch.setattr(interventions, "requests", http)
    ctx = interventions.preflight(_confirmed(customer_id="1234567890"))
    assert ctx is not None and ctx.customer_id == "1234567890"


# ---------------------------------------------------------------------------
# lite mode — journal line, no HTTP
# ---------------------------------------------------------------------------
def test_lite_appends_journal_line_without_http(monkeypatch, tmp_path):
    journal = tmp_path / "journal" / "cutegory.md"
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    monkeypatch.setenv("GL_ADS_JOURNAL_FILE", str(journal))
    monkeypatch.setenv("GL_ADS_AGENT", "Petr")
    monkeypatch.delenv("GL_ADS_URL", raising=False)
    http = _Http()
    monkeypatch.setattr(interventions, "requests", http)

    cli._dispatch(_confirmed(ticket=None, func=lambda a: None))
    line = journal.read_text(encoding="utf-8")
    assert re.match(r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} · Petr \(cli/google-ads-app\) · "
                    r"google 1234567890 · campaign-status · --confirm\. Důvod: " + re.escape(WHY)
                    + r"\.\n$", line)
    assert http.gets == [] and http.posts == []


def test_lite_requires_journal_file(monkeypatch):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    monkeypatch.delenv("GL_ADS_JOURNAL_FILE", raising=False)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed(ticket=None))
    assert exc.value.code == 2


def test_lite_still_requires_why(monkeypatch, tmp_path):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    monkeypatch.setenv("GL_ADS_JOURNAL_FILE", str(tmp_path / "j.md"))
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed(ticket=None, why=None))
    assert exc.value.code == 2 and not (tmp_path / "j.md").exists()


# ---------------------------------------------------------------------------
# off mode + invalid mode
# ---------------------------------------------------------------------------
def test_off_mode_warns_and_runs(monkeypatch, capsys):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "off")
    ran = []
    cli._dispatch(_confirmed(ticket=None, func=lambda a: ran.append(a)))
    assert len(ran) == 1
    assert "GL_ADS_TICKET_GATE=off" in capsys.readouterr().err


def test_invalid_mode_exits_2(monkeypatch):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "yolo")
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# record_result covers atomic mutate responses
# ---------------------------------------------------------------------------
def test_record_result_reads_mutate_operation_responses(fake_client):
    interventions.reset_collected()
    resp = fake_client.get_type("MutateGoogleAdsResponse")
    r = fake_client.get_type("MutateOperationResponse")
    r.campaign_result.resource_name = "customers/1/campaigns/9"
    resp.mutate_operation_responses.append(r)
    interventions.record_result(resp)
    assert interventions.collected() == ["customers/1/campaigns/9"]


def test_record_result_never_raises():
    interventions.reset_collected()
    interventions.record_result(object())
    interventions.record_result(None)
    assert interventions.collected() == []


# ---------------------------------------------------------------------------
# _run_mutation collects only on confirm
# ---------------------------------------------------------------------------
def test_run_mutation_collects_resource_names_on_confirm(fake_client, recorder, monkeypatch):
    from gads import api
    interventions.reset_collected()
    op = fake_client.get_type("CampaignOperation")
    op.update.resource_name = "customers/1234567890/campaigns/5"
    api._run_mutation(fake_client, None, "1234567890", service_name="CampaignService",
                      method_name="mutate_campaigns", request_type="MutateCampaignsRequest",
                      operations=[op], confirm=False)
    assert interventions.collected() == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------
def test_write_commands_expose_ticket_and_why():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    write_cmds = [n for n, p in sub.choices.items()
                  if any(o.dest == "confirm" for o in p._actions)]
    assert write_cmds, "no write commands found"
    for name in write_cmds:
        dests = {o.dest for o in sub.choices[name]._actions}
        assert {"ticket", "why"} <= dests, name
    read_cmds = [n for n, p in sub.choices.items() if n not in write_cmds]
    for name in read_cmds:
        dests = {o.dest for o in sub.choices[name]._actions}
        assert "ticket" not in dests, name
