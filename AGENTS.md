# AGENTS.md — google-ads-app (GrowLead fork)

> Primary context: [CLAUDE.md](./CLAUDE.md) (kód, příkazy, auth). Platformní kontext: [../AGENTS.md](../AGENTS.md) (GL-ADS, PMRS parent).
>
> Fork `faborsky/google-ads-app` (MIT). Remote `origin` = grow-lead-agency/google-ads-app, `upstream` = faborsky. Default větev `growlead-safety`: nové entity PAUSED (`--enabled` opt-in), `conversion-update --status` jen enabled/hidden. Opravy z upstreamu: `git fetch upstream && git merge upstream/main`.

## Testy a CI (GrowLead tooling, GRO-1302)

Python Standard v1, adopce vlna 1. Tooling je jen v nových souborech, které upstream nemá:
`pyproject.toml`, `uv.lock`, `.python-version` (3.12), `Makefile`, `.github/workflows/{ci,security}.yml`,
`.github/scripts/check_requirements_sync.py`. Upstream soubory (`requirements*.txt`, `setup.sh`, `run.sh`,
`CLAUDE.md`, kód) se tooling PR nemění.

| Příkaz | Co dělá |
|---|---|
| `make setup` | `uv sync --locked` (Python 3.12 z `.python-version`) |
| `make test` | `uv run pytest -q`: offline sada, reálný `GoogleAdsClient` na v25 stubech s fake credentials, gRPC metody nahrazené recorderem (`tests/conftest.py`). Žádná síť, žádný `.env`, žádná `.quota/` |
| `make lint` | `uv run ruff check .`, ve vlně 1 jen syntaktické chyby (`E9,F63,F7,F82`) |
| `make deps-check` | `uv lock --check` + `requirements*.txt` musí sedět 1:1 s `pyproject.toml` |
| `make docs-check` | upstream `scripts/check_docs_consistency.py` |
| `make check` | deps-check + lint + test + docs-check = brána před commitem a před "hotovo" |
| `make audit` | pip-audit nad `uv export --no-dev` (+ gitleaks, pokud je nainstalovaný) |
| `make lock` | `uv lock` |

Záměrně chybí `make fmt` (ruff format ani `ruff check --fix` na upstream kód nepouštět, rozbilo by merge
z upstreamu) a `make type` (pyrefly je vlna 4).

**Co je zápis do produkce:** jakýkoli příkaz CLI s `--confirm` proti skutečnému účtu (`./run.sh ... --confirm
--ticket <id> --why "..."`) mění klientský Google Ads účet. Bez `--confirm` jde o `validate_only` dry-run,
`recommendation-apply/dismiss` validate_only nemají (dry-run = jen plán). Zápis jen ze session se schválením
podle `../AGENTS.md` (permission tier "ask", Cutegory navíc Martin).

**CI nikdy nevolá reálné Google Ads API:** workflow nemá žádná tajemství (`permissions: contents: read`,
`persist-credentials: false`), testy běží proti fake klientovi. Runner je natvrdo `ubuntu-latest`, protože repo
je veřejné a org proměnná `CI_RUNNER` míří na self-hosted runner. Nový test nesmí sahat na síť ani na
credentials; kdyby musel, dostane marker `integration` a do CI nepatří.

**Upstream merge a závislosti:** runtime (`setup.sh`) instaluje z `requirements.txt`, CI z `uv.lock`. Když merge
z upstreamu změní `requirements*.txt`, CI spadne na `deps-check`: dorovnat `pyproject.toml`, `make lock`,
commitnout `uv.lock`. `--locked` z CI nikdy neodebírat.
