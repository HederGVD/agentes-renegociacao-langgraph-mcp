"""Servidor MCP que expõe o "ERP" da distribuidora (SQLite) como ferramentas.

Os agentes nunca tocam no banco diretamente: tudo passa por estas ferramentas MCP,
do mesmo jeito que passariam por um ERP/CRM real da empresa.
"""
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(os.environ.get("ERP_DB", Path(__file__).parent / "data" / "erp.db"))

mcp = FastMCP("erp-distribuidora", log_level="WARNING")


def _conn() -> sqlite3.Connection:
    novo = not DB_PATH.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    if novo:
        _criar_e_popular(c)
    return c


def _criar_e_popular(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE clientes (id INTEGER PRIMARY KEY, nome TEXT, cnpj TEXT,
                               segmento TEXT, cliente_desde TEXT, email TEXT);
        CREATE TABLE titulos (id INTEGER PRIMARY KEY, cliente_id INTEGER, valor REAL,
                              vencimento TEXT, pago_em TEXT);
        CREATE TABLE acordos (id INTEGER PRIMARY KEY, cliente_id INTEGER, valor_original REAL,
                              desconto_pct REAL, parcelas INTEGER, valor_final REAL,
                              aprovado_por TEXT, criado_em TEXT);
        CREATE TABLE boletos (id INTEGER PRIMARY KEY, acordo_id INTEGER, numero INTEGER,
                              valor REAL, vencimento TEXT);
        CREATE TABLE mensagens (id INTEGER PRIMARY KEY, cliente_id INTEGER, canal TEXT,
                                texto TEXT, enviada_em TEXT);
        """
    )
    hoje = date.today()
    d = lambda dias: (hoje - timedelta(days=dias)).isoformat()  # noqa: E731
    c.executemany(
        "INSERT INTO clientes VALUES (?,?,?,?,?,?)",
        [
            (1, "Mercadinho Boa Vista", "12.345.678/0001-90", "varejo", "2017-03-10", "financeiro@boavista.com.br"),
            (2, "Padaria Trigo de Ouro", "23.456.789/0001-01", "food service", "2023-08-02", "contato@trigodeouro.com.br"),
            (3, "Restaurante Sabor da Serra", "34.567.890/0001-12", "food service", "2020-01-15", "adm@saborserra.com.br"),
        ],
    )
    c.executemany(
        "INSERT INTO titulos (cliente_id, valor, vencimento, pago_em) VALUES (?,?,?,?)",
        [
            # cliente 1: bom pagador antigo, atrasou pouco
            (1, 3200.0, d(400), d(398)), (1, 2900.0, d(300), d(300)), (1, 4100.0, d(200), d(195)),
            (1, 3800.0, d(40), None), (1, 2500.0, d(10), None),
            # cliente 2: novo, histórico ruim, dívida alta
            (2, 1500.0, d(250), d(210)), (2, 1800.0, d(180), d(120)),
            (2, 6200.0, d(150), None), (2, 7400.0, d(95), None), (2, 5900.0, d(60), None),
            # cliente 3: médio
            (3, 2200.0, d(300), d(290)), (3, 2600.0, d(200), d(170)),
            (3, 4800.0, d(75), None), (3, 3900.0, d(45), None),
        ],
    )
    c.commit()


def _hoje() -> str:
    return date.today().isoformat()


@mcp.tool()
def buscar_cliente(cliente_id: int) -> str:
    """Dados cadastrais de um cliente."""
    with _conn() as c:
        row = c.execute("SELECT * FROM clientes WHERE id=?", (cliente_id,)).fetchone()
    return json.dumps(dict(row) if row else {"erro": "cliente não encontrado"}, ensure_ascii=False)


@mcp.tool()
def titulos_em_aberto(cliente_id: int) -> str:
    """Títulos vencidos e não pagos, com dias de atraso."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, valor, vencimento FROM titulos WHERE cliente_id=? AND pago_em IS NULL",
            (cliente_id,),
        ).fetchall()
    hoje = date.today()
    return json.dumps(
        [{**dict(r), "dias_atraso": (hoje - date.fromisoformat(r["vencimento"])).days} for r in rows]
    )


@mcp.tool()
def historico_pagamentos(cliente_id: int) -> str:
    """Títulos já pagos, com quantos dias de atraso cada um foi pago."""
    with _conn() as c:
        rows = c.execute(
            "SELECT valor, vencimento, pago_em FROM titulos WHERE cliente_id=? AND pago_em IS NOT NULL",
            (cliente_id,),
        ).fetchall()
    return json.dumps(
        [
            {
                "valor": r["valor"],
                "atraso_dias": (date.fromisoformat(r["pago_em"]) - date.fromisoformat(r["vencimento"])).days,
            }
            for r in rows
        ]
    )


@mcp.tool()
def registrar_acordo(cliente_id: int, valor_original: float, desconto_pct: float,
                     parcelas: int, aprovado_por: str) -> str:
    """Grava o acordo de renegociação, gera os boletos mensais e baixa os títulos renegociados."""
    valor_final = round(valor_original * (1 - desconto_pct / 100), 2)
    parcela = round(valor_final / parcelas, 2)
    hoje = date.today()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO acordos (cliente_id, valor_original, desconto_pct, parcelas, valor_final,"
            " aprovado_por, criado_em) VALUES (?,?,?,?,?,?,?)",
            (cliente_id, valor_original, desconto_pct, parcelas, valor_final, aprovado_por, _hoje()),
        )
        acordo_id = cur.lastrowid
        boletos = []
        for n in range(1, parcelas + 1):
            venc = (hoje + timedelta(days=30 * n)).isoformat()
            c.execute("INSERT INTO boletos (acordo_id, numero, valor, vencimento) VALUES (?,?,?,?)",
                      (acordo_id, n, parcela, venc))
            boletos.append({"numero": n, "valor": parcela, "vencimento": venc})
        c.execute("UPDATE titulos SET pago_em=? WHERE cliente_id=? AND pago_em IS NULL",
                  (f"acordo-{acordo_id}", cliente_id))
    return json.dumps({"acordo_id": acordo_id, "valor_final": valor_final, "boletos": boletos})


@mcp.tool()
def enviar_mensagem(cliente_id: int, canal: str, texto: str) -> str:
    """Envia uma mensagem ao cliente (aqui: grava na caixa de saída do ERP)."""
    with _conn() as c:
        cur = c.execute("INSERT INTO mensagens (cliente_id, canal, texto, enviada_em) VALUES (?,?,?,?)",
                        (cliente_id, canal, texto, _hoje()))
    return json.dumps({"mensagem_id": cur.lastrowid, "status": "enviada"})


if __name__ == "__main__":
    mcp.run()  # stdio
