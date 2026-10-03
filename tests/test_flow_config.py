#!/usr/bin/env python3
"""Offline tests for scripts/flow_config.py (python3 tests/test_flow_config.py; pytest works too).

- config.example.yaml is exactly flow_config.DEFAULTS (one source of truth for the defaults);
- the example validates, every text template formats, placeholders count as empty;
- a local config only needs the keys that differ (deep merge), and discord.json + config IDs are combined.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="flow-config-test-"))
os.environ["HERMES_HOME"] = str(TMP / ".hermes")
os.environ["HERMES_FLOW_CONFIG"] = str(REPO / "config.example.yaml")
sys.path.insert(0, str(REPO / "scripts"))
import yaml  # noqa: E402

import flow_config as fc  # noqa: E402


def test_example_is_defaults():
    assert yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8")) == fc.DEFAULTS


def test_example_validates():
    assert fc.validate() == [], fc.validate()


def test_texts_format():
    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, str):
            yield path
    values = dict(tijd="10:00", tekst="t", project="P", id="t_1", titel="T", url="https://x.example.invalid", urls="u",
                  aantal=2, niveau="Fase", code="F1", eind="t_9", waarom="w", kaarten="t_1", uren=6, link="l",
                  label="L", detail="d", sinds="09:00", status="failed/dead", host="h", vrij=1.0, totaal=2.0,
                  procent=50.0, drempel=10.0, nr="2026-01-01.1", notitie="n", score="1/1", datum="01-01",
                  modus="opstart", tot="2026-01-31", reden="r", prefix="Vraag voor de eigenaar:", branch="staging", commit="abc",
                  wie="manager", wanneer="01-01 10:00", oplossing="o", oudste="01-01 09:00", weg=1, bewaard=2,
                  naam="Claude", reset="ma 10:00", stop=90)
    for path in walk(fc.get("teksten"), ""):
        fc.text(path, **values)  # raises on an unknown placeholder


def test_placeholders_and_merge():
    assert not fc.is_set("<GUILD_ID>") and not fc.is_set("") and fc.is_set("123")
    cfg = TMP / "flow.yaml"
    cfg.write_text("eigenaar:\n  naam: Sanne\ndiscord:\n  owner_id: '77'\n  kanalen:\n    meldingen:\n      id: '88'\n")
    (TMP / ".hermes" / "team").mkdir(parents=True, exist_ok=True)
    (TMP / ".hermes" / "team" / "discord.json").write_text(json.dumps({"guild": "1", "owner": "2", "vragen": "3"}))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "flow_config.py"), "kanalen"], capture_output=True,
                       text=True, env={**os.environ, "HERMES_FLOW_CONFIG": str(cfg)}, check=True)
    ids = dict(line.split() for line in r.stdout.splitlines())
    assert ids == {"guild": "1", "owner": "77", "vragen": "3", "meldingen": "88"}, ids
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "flow_config.py"), "get", "discord.kanalen.vragen.naam"],
                       capture_output=True, text=True, env={**os.environ, "HERMES_FLOW_CONFIG": str(cfg)}, check=True)
    assert r.stdout.strip() == "vragen"  # not in the local file: default kept


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok    {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FOUT  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} geslaagd")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
