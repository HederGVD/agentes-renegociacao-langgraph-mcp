import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(RAIZ))

from agentes import brl, calcular_score, checar_politica, proposta_por_regras


@pytest.fixture(autouse=True)
def banco_temporario(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_DB", str(tmp_path / "erp.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield tmp_path / "erp.db"


def test_politica_bloqueia_fora_da_alcada():
    assert checar_politica({"desconto_pct": 8, "parcelas": 3}) == []
    assert len(checar_politica({"desconto_pct": 15, "parcelas": 10})) == 2


def test_score_bom_pagador_maior_que_mau_pagador():
    bom = calcular_score([{"atraso_dias": 2}], [{"dias_atraso": 10}], anos_cliente=8)
    ruim = calcular_score([{"atraso_dias": 50}], [{"dias_atraso": 150}], anos_cliente=1)
    assert bom > ruim


def test_proposta_risco_alto_exige_humano():
    assert checar_politica(proposta_por_regras(score=10, divida=20000).model_dump())


def test_brl():
    assert brl(19500) == "R$ 19.500,00"


def _rodar(cliente, auto):
    from main import executar
    return asyncio.run(executar(cliente, auto))


def test_cliente_confiavel_fecha_acordo_sem_humano(banco_temporario):
    estado = _rodar(1, auto=None)
    assert estado["decisao"] == "automatico"
    with sqlite3.connect(banco_temporario) as c:
        assert c.execute("SELECT COUNT(*) FROM boletos").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM titulos WHERE cliente_id=1 AND pago_em IS NULL").fetchone()[0] == 0


def test_cliente_arriscado_rejeitado_nao_gera_acordo(banco_temporario):
    estado = _rodar(2, auto="rejeitar")
    assert estado["decisao"] == "rejeitado" and "acordo" not in estado
    with sqlite3.connect(banco_temporario) as c:
        assert c.execute("SELECT COUNT(*) FROM acordos").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM mensagens").fetchone()[0] == 1


def test_cliente_arriscado_aprovado_registra_quem_aprovou(banco_temporario):
    estado = _rodar(2, auto="aprovar")
    assert estado["decisao"] == "aprovado"
    with sqlite3.connect(banco_temporario) as c:
        assert c.execute("SELECT aprovado_por FROM acordos").fetchone()[0] == "demo"
