import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentes import montar_grafo

SCRIPT_SERVIDOR = str(Path(__file__).parent / "erp_mcp_server.py")


def perguntar_supervisor(pedido: dict) -> dict:
    print("\n=== APROVAÇÃO NECESSÁRIA ===")
    print(json.dumps(pedido, indent=2, ensure_ascii=False))
    acao = input("aprovar / rejeitar / contraproposta? ").strip().lower()
    nome = input("Seu nome: ").strip() or "supervisor"
    if acao.startswith("c"):
        return {"acao": "contraproposta", "supervisor": nome,
                "desconto_pct": input("desconto %: "), "parcelas": input("parcelas: ")}
    return {"acao": "aprovar" if acao.startswith("a") else "rejeitar", "supervisor": nome}


async def executar(cliente_id: int, auto: str | None = None) -> dict:
    servidor = StdioServerParameters(command=sys.executable, args=[SCRIPT_SERVIDOR], env=dict(os.environ))
    async with stdio_client(servidor) as (r, w), ClientSession(r, w) as sessao:
        await sessao.initialize()
        tools = {t.name: t for t in await load_mcp_tools(sessao)}
        grafo = montar_grafo(tools, InMemorySaver())
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}

        estado = await grafo.ainvoke({"cliente_id": cliente_id, "log": []}, cfg)
        while "__interrupt__" in estado:
            pedido = estado["__interrupt__"][0].value
            resposta = {"acao": auto, "supervisor": "demo"} if auto else perguntar_supervisor(pedido)
            estado = await grafo.ainvoke(Command(resume=resposta), cfg)
        return estado


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cliente", type=int, required=True)
    ap.add_argument("--auto", choices=["aprovar", "rejeitar"])
    a = ap.parse_args()
    estado = asyncio.run(executar(a.cliente, a.auto))
    print("\n".join(estado["log"]))
    print("\nMensagem ao cliente:\n" + estado["mensagem"])


if __name__ == "__main__":
    main()
