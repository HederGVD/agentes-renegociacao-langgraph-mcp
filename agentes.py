from __future__ import annotations

import json
import os
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field


DESCONTO_MAX_AUTONOMO = 10.0
PARCELAS_MAX_AUTONOMO = 6
DESCONTO_MAX_ABSOLUTO = 30.0
PARCELAS_MAX_ABSOLUTO = 12
MODELO = os.environ.get("MODELO_LLM", "claude-sonnet-5-5")


class Proposta(BaseModel):
    desconto_pct: float = Field(ge=0, le=DESCONTO_MAX_ABSOLUTO)
    parcelas: int = Field(ge=1, le=PARCELAS_MAX_ABSOLUTO)
    justificativa: str


class Estado(TypedDict, total=False):
    cliente_id: int
    cliente: dict
    divida_total: float
    score: int
    analise: str
    proposta: dict
    exige_humano: bool
    motivos_humano: list[str]
    decisao: Literal["aprovado", "rejeitado", "automatico"]
    aprovado_por: str
    acordo: dict
    mensagem: str
    log: list[str]


def brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def chamar(tools: dict, nome: str, **args):
    res = await tools[nome].ainvoke(args)
    if isinstance(res, list):
        res = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in res)
    return json.loads(res)


def _llm():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=MODELO, temperature=0)


def calcular_score(historico: list[dict], abertos: list[dict], anos_cliente: float) -> int:
    score = 60
    if historico:
        atraso_medio = sum(h["atraso_dias"] for h in historico) / len(historico)
        score -= min(30, int(atraso_medio / 2))
    score += min(20, int(anos_cliente * 3))
    pior_atraso = max((t["dias_atraso"] for t in abertos), default=0)
    score -= min(30, pior_atraso // 5)
    return max(0, min(100, score))


def proposta_por_regras(score: int, divida: float) -> Proposta:
    if score >= 70:
        return Proposta(desconto_pct=8, parcelas=3, justificativa="Cliente confiável: desconto moderado e prazo curto.")
    if score >= 45:
        return Proposta(desconto_pct=10, parcelas=6, justificativa="Risco médio: desconto no teto autônomo, 6 parcelas.")
    parcelas = 10 if divida > 15000 else 8
    return Proposta(desconto_pct=15, parcelas=parcelas,
                    justificativa="Risco alto e dívida antiga: melhor recuperar parte do valor do que perder tudo.")


def checar_politica(p: dict) -> list[str]:
    motivos = []
    if p["desconto_pct"] > DESCONTO_MAX_AUTONOMO:
        motivos.append(f"desconto {p['desconto_pct']}% acima de {DESCONTO_MAX_AUTONOMO}%")
    if p["parcelas"] > PARCELAS_MAX_AUTONOMO:
        motivos.append(f"{p['parcelas']} parcelas acima de {PARCELAS_MAX_AUTONOMO}")
    return motivos


def montar_grafo(tools: dict, checkpointer):
    async def analista_credito(s: Estado) -> Estado:
        cid = s["cliente_id"]
        cliente = await chamar(tools, "buscar_cliente", cliente_id=cid)
        if "erro" in cliente:
            raise ValueError(cliente["erro"])
        abertos = await chamar(tools, "titulos_em_aberto", cliente_id=cid)
        historico = await chamar(tools, "historico_pagamentos", cliente_id=cid)
        from datetime import date
        anos = (date.today() - date.fromisoformat(cliente["cliente_desde"])).days / 365
        divida = round(sum(t["valor"] for t in abertos), 2)
        score = calcular_score(historico, abertos, anos)
        analise = (f"{cliente['nome']}: {len(abertos)} títulos em aberto, {brl(divida)}, "
                   f"cliente há {anos:.1f} anos, score {score}/100.")
        return {"cliente": cliente, "divida_total": divida, "score": score, "analise": analise,
                "log": [f"[analista_credito] {analise}"]}

    async def negociador(s: Estado) -> Estado:
        llm = _llm()
        if llm:
            prompt = (
                "Você é negociador de cobrança de uma distribuidora de alimentos. Proponha um acordo "
                "que maximize a recuperação sem perder o cliente. Política: até "
                f"{DESCONTO_MAX_AUTONOMO}% de desconto e {PARCELAS_MAX_AUTONOMO} parcelas sem aprovação; "
                f"teto absoluto {DESCONTO_MAX_ABSOLUTO}% e {PARCELAS_MAX_ABSOLUTO} parcelas.\n"
                f"Análise de crédito: {s['analise']}"
            )
            p = await llm.with_structured_output(Proposta).ainvoke(prompt)
            origem = "llm"
        else:
            p = proposta_por_regras(s["score"], s["divida_total"])
            origem = "regras"
        prop = p.model_dump()
        return {"proposta": prop,
                "log": s["log"] + [f"[negociador/{origem}] {prop['desconto_pct']}% em {prop['parcelas']}x — {prop['justificativa']}"]}

    def guardiao_politica(s: Estado) -> Estado:
        motivos = checar_politica(s["proposta"])
        msg = "precisa de aprovação: " + "; ".join(motivos) if motivos else "dentro da alçada, segue automático"
        return {"exige_humano": bool(motivos), "motivos_humano": motivos,
                "log": s["log"] + [f"[guardiao_politica] {msg}"]}

    def supervisor_humano(s: Estado) -> Estado:
        resposta = interrupt({
            "analise": s["analise"],
            "proposta": s["proposta"],
            "motivos": s["motivos_humano"],
            "opcoes": "aprovar | rejeitar | contraproposta (desconto_pct, parcelas)",
        })
        acao = resposta.get("acao")
        quem = resposta.get("supervisor", "supervisor")
        if acao == "aprovar":
            return {"decisao": "aprovado", "aprovado_por": quem,
                    "log": s["log"] + [f"[supervisor_humano] {quem} aprovou"]}
        if acao == "contraproposta":
            prop = {**s["proposta"], "desconto_pct": float(resposta["desconto_pct"]),
                    "parcelas": int(resposta["parcelas"]),
                    "justificativa": f"Contraproposta de {quem}"}
            Proposta(**prop)
            return {"proposta": prop, "aprovado_por": quem, "decisao": "aprovado",
                    "log": s["log"] + [f"[supervisor_humano] {quem} contrapropôs {prop['desconto_pct']}% em {prop['parcelas']}x"]}
        return {"decisao": "rejeitado", "aprovado_por": quem,
                "log": s["log"] + [f"[supervisor_humano] {quem} rejeitou"]}

    async def executor(s: Estado) -> Estado:
        p = s["proposta"]
        acordo = await chamar(tools, "registrar_acordo", cliente_id=s["cliente_id"],
                              valor_original=s["divida_total"], desconto_pct=p["desconto_pct"],
                              parcelas=p["parcelas"], aprovado_por=s.get("aprovado_por", "agente"))
        return {"acordo": acordo, "decisao": s.get("decisao", "automatico"),
                "log": s["log"] + [f"[executor] acordo #{acordo['acordo_id']} gravado, {len(acordo['boletos'])} boletos gerados"]}

    async def comunicador(s: Estado) -> Estado:
        nome = s["cliente"]["nome"]
        if s.get("acordo"):
            a = s["acordo"]
            base = (f"Olá, {nome}! Conseguimos um acordo para regularizar seus títulos: "
                    f"{brl(a['valor_final'])} em {len(a['boletos'])}x de {brl(a['boletos'][0]['valor'])}, "
                    f"primeiro vencimento em {a['boletos'][0]['vencimento']}. Os boletos seguem em anexo.")
        else:
            base = (f"Olá, {nome}. Ainda não conseguimos fechar uma condição para os títulos em aberto "
                    f"({brl(s['divida_total'])}). Um analista entrará em contato para conversar.")
        llm = _llm()
        texto = (await llm.ainvoke(f"Reescreva de forma cordial e curta, sem mudar valores nem datas:\n{base}")).content if llm else base
        await chamar(tools, "enviar_mensagem", cliente_id=s["cliente_id"], canal="email", texto=texto)
        return {"mensagem": texto, "log": s["log"] + ["[comunicador] mensagem enviada ao cliente"]}

    g = StateGraph(Estado)
    for nome, fn in [("analista_credito", analista_credito), ("negociador", negociador),
                     ("guardiao_politica", guardiao_politica), ("supervisor_humano", supervisor_humano),
                     ("executor", executor), ("comunicador", comunicador)]:
        g.add_node(nome, fn)
    g.add_edge(START, "analista_credito")
    g.add_edge("analista_credito", "negociador")
    g.add_edge("negociador", "guardiao_politica")
    g.add_conditional_edges("guardiao_politica",
                            lambda s: "supervisor_humano" if s["exige_humano"] else "executor")
    g.add_conditional_edges("supervisor_humano",
                            lambda s: "executor" if s["decisao"] == "aprovado" else "comunicador")
    g.add_edge("executor", "comunicador")
    g.add_edge("comunicador", END)
    return g.compile(checkpointer=checkpointer)
