#!/usr/bin/env python3
"""
Relatório bancário diário — saldo e movimentações dos últimos 7 dias.

Fonte de dados: API da Pluggy (Open Finance Brasil, via Meu Pluggy).
Somente leitura: o script NÃO inicia pagamentos nem altera nada nas contas.

Uso:
    python relatorio_bancario.py            # busca dados reais e envia o e-mail
    python relatorio_bancario.py --demo     # gera relatorio_demo.html com dados fictícios
    python relatorio_bancario.py --no-send  # busca dados reais e só salva o HTML

Variáveis de ambiente (no GitHub ficam em Settings > Secrets):
    PLUGGY_CLIENT_ID, PLUGGY_CLIENT_SECRET   credenciais da aplicação na Pluggy
    PLUGGY_ITEM_IDS                          IDs das conexões (um por banco), separados por vírgula
    EMAIL_REMETENTE                          seu Gmail que vai enviar (ex.: voce@gmail.com)
    EMAIL_SENHA_APP                          senha de app do Gmail (16 letras)
    EMAIL_DESTINO                            quem recebe (pode ser o mesmo e-mail)
    SMTP_HOST, SMTP_PORT                     opcional, padrão smtp.gmail.com:465
    DIAS                                     opcional, padrão 7

Privacidade: no modo normal (envio do e-mail) o script não escreve saldos nem valores
no log, porque o log do GitHub Actions fica visível para quem acessa o repositório.
"""

import argparse
import html
import os
import random
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

API = "https://api.pluggy.ai"
BRT = timezone(timedelta(hours=-3))  # Brasil não tem horário de verão desde 2019

TIPOS_CONTA = {
    "CHECKING_ACCOUNT": "Conta corrente",
    "SAVINGS_ACCOUNT": "Poupança",
    "CREDIT_CARD": "Cartão de crédito",
}


# ----------------------------------------------------------------------------
# Coleta de dados
# ----------------------------------------------------------------------------

def coletar_pluggy(dias):
    import requests  # importado aqui para o modo demo não depender da lib

    client_id = os.environ["PLUGGY_CLIENT_ID"]
    client_secret = os.environ["PLUGGY_CLIENT_SECRET"]
    item_ids = [i.strip() for i in os.environ["PLUGGY_ITEM_IDS"].split(",") if i.strip()]

    r = requests.post(f"{API}/auth", json={"clientId": client_id, "clientSecret": client_secret}, timeout=30)
    r.raise_for_status()
    s = requests.Session()
    s.headers["X-API-KEY"] = r.json()["apiKey"]

    hoje = datetime.now(BRT).date()
    inicio = hoje - timedelta(days=dias)
    # pede um dia a mais nas pontas porque as datas da API vêm em UTC
    params_base = {"dateFrom": (inicio - timedelta(days=1)).isoformat(),
                   "dateTo": (hoje + timedelta(days=3)).isoformat()}  # +3 pega agendados próximos

    conexoes = []
    for item_id in item_ids:
        con = {"item_id": item_id, "banco": item_id[:8], "status": "?", "atualizado": None,
               "erro": None, "contas": []}
        try:
            it = s.get(f"{API}/items/{item_id}", timeout=30)
            it.raise_for_status()
            it = it.json()
            con["banco"] = (it.get("connector") or {}).get("name") or con["banco"]
            con["status"] = it.get("status") or "?"
            con["atualizado"] = it.get("lastUpdatedAt") or it.get("updatedAt")

            ac = s.get(f"{API}/accounts", params={"itemId": item_id}, timeout=30)
            ac.raise_for_status()
            for a in ac.json().get("results", []):
                conta = {
                    "id": a["id"],
                    "nome": a.get("marketingName") or a.get("name") or "Conta",
                    "numero": a.get("number") or "",
                    "tipo": a.get("type"),            # BANK ou CREDIT
                    "subtipo": a.get("subtype"),
                    "saldo": float(a.get("balance") or 0),
                    "limite_disponivel": (a.get("creditData") or {}).get("availableCreditLimit"),
                    "fatura": None,
                    "transacoes": [],
                }
                if conta["tipo"] == "CREDIT":
                    conta["fatura"] = ultima_fatura(s, a)
                # /v2/transactions: paginação por cursor; "next" já vem como query string pronta
                url = f"{API}/v2/transactions"
                params = {**params_base, "accountId": a["id"]}
                for _ in range(50):  # limite de segurança
                    tx = s.get(url, params=params, timeout=60)
                    tx.raise_for_status()
                    j = tx.json()
                    for t in j.get("results", []):
                        conta["transacoes"].append({
                            "data": t["date"],
                            "descricao": t.get("description") or "",
                            "valor": abs(float(t.get("amount") or 0)),
                            "tipo": t.get("type"),          # DEBIT = saída, CREDIT = entrada
                            "status": t.get("status") or "POSTED",
                        })
                    nxt = j.get("next")
                    if not nxt:
                        break
                    url, params = f"{API}/v2/transactions{nxt}", None
                con["contas"].append(conta)
            # Itens do Meu Pluggy vêm com o nome "MeuPluggy": usa o nome do banco da própria conta
            if "pluggy" in con["banco"].lower() and con["contas"]:
                con["banco"] = nome_banco(con["contas"])
        except Exception as e:  # um banco com problema não derruba o relatório inteiro
            detalhe = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                detalhe = f" | resposta: {resp.text[:200]}"
            con["erro"] = (str(e) + detalhe)[:400]
            print(f"[ERRO] item {item_id[:8]}…: {con['erro']}", file=sys.stderr)
        conexoes.append(con)
    return conexoes


def ultima_fatura(s, a):
    """Fatura fechada mais recente do cartão: {"valor", "vencimento"} ou None."""
    try:
        b = s.get(f"{API}/bills", params={"accountId": a["id"]}, timeout=30)
        b.raise_for_status()
        bills = [x for x in b.json().get("results", []) if x.get("dueDate")]
        if bills:
            ult = max(bills, key=lambda x: x["dueDate"])
            return {"valor": float(ult.get("totalAmount") or 0), "vencimento": ult["dueDate"]}
    except Exception as e:
        print(f"[AVISO] faturas do cartão {a.get('name')}: {e}", file=sys.stderr)
    return None


def nome_banco(contas):
    """'Banco Santander Pessoa Física' -> 'Santander'; senão, 1ª palavra do nome do cartão."""
    import re
    bancos = [c for c in contas if c["tipo"] == "BANK"] or contas
    nome = bancos[0]["nome"]
    nome = re.sub(r"(?i)^banco\s+", "", nome)
    nome = re.sub(r"(?i)\s*(pessoa f[íi]sica|pessoa jur[íi]dica|s\.?a\.?)\s*$", "", nome).strip()
    if bancos[0]["tipo"] != "BANK":
        nome = nome.split()[0]
    return nome.title() if nome.isupper() else nome


def coletar_demo(dias):
    """Dados fictícios, com semente fixa, para testar o layout e as contas."""
    rnd = random.Random(42)
    agora = datetime.now(BRT)
    desc_saida = ["PIX ENVIADO MERCADO", "COMPRA CARTAO DEB POSTO", "PAG BOLETO ENERGIA",
                  "PIX ENVIADO ESCOLA", "TARIFA PACOTE SERVICOS", "COMPRA DEB FARMACIA",
                  "PAG CONTA AGUA", "PIX ENVIADO ACADEMIA"]
    desc_entrada = ["PIX RECEBIDO", "TED RECEBIDA", "RENDIMENTO APLIC", "SALARIO"]
    desc_cartao = ["IFOOD", "UBER", "AMAZON", "PADARIA", "NETFLIX", "RESTAURANTE"]

    def gerar(n, descs, tipo, faixa):
        out = []
        for _ in range(n):
            quando = agora - timedelta(days=rnd.randint(0, dias), hours=rnd.randint(0, 12))
            out.append({"data": quando.astimezone(timezone.utc).isoformat(),
                        "descricao": rnd.choice(descs),
                        "valor": round(rnd.uniform(*faixa), 2),
                        "tipo": tipo, "status": "POSTED"})
        return out

    ontem = (agora - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    bradesco_cc = gerar(9, desc_saida, "DEBIT", (20, 900)) + gerar(2, desc_entrada, "CREDIT", (100, 3000))
    bradesco_cc.append({"data": ontem, "descricao": "SALARIO", "valor": 12500.00, "tipo": "CREDIT", "status": "POSTED"})
    santander_cc = gerar(6, desc_saida, "DEBIT", (15, 600)) + gerar(1, desc_entrada, "CREDIT", (50, 800))
    santander_cc.append({"data": (agora + timedelta(days=2)).astimezone(timezone.utc).isoformat(),
                         "descricao": "PAG AGENDADO CONDOMINIO", "valor": 1350.00,
                         "tipo": "DEBIT", "status": "PENDING"})
    cartao = gerar(8, desc_cartao, "DEBIT", (12, 450))

    return [
        {"item_id": "demo-1", "banco": "Bradesco", "status": "UPDATED",
         "atualizado": (agora - timedelta(hours=5)).isoformat(), "erro": None,
         "contas": [{"id": "a1", "nome": "Conta Corrente", "numero": "1234/56789-0", "tipo": "BANK",
                     "subtipo": "CHECKING_ACCOUNT", "saldo": 8432.17, "limite_disponivel": None,
                     "transacoes": bradesco_cc}]},
        {"item_id": "demo-2", "banco": "Santander", "status": "UPDATED",
         "atualizado": (agora - timedelta(hours=7)).isoformat(), "erro": None,
         "contas": [{"id": "a2", "nome": "Conta Corrente", "numero": "0001/01002345-6", "tipo": "BANK",
                     "subtipo": "CHECKING_ACCOUNT", "saldo": 2210.55, "limite_disponivel": None,
                     "transacoes": santander_cc},
                    {"id": "a3", "nome": "Santander Unique", "numero": "**** 4417", "tipo": "CREDIT",
                     "subtipo": "CREDIT_CARD", "saldo": 3187.90, "limite_disponivel": 11812.10,
                     "fatura": {"valor": 1240.37, "vencimento": (agora + timedelta(days=6)).isoformat()},
                     "transacoes": cartao},
                    {"id": "a4", "nome": "Santander Elite", "numero": "8021", "tipo": "CREDIT",
                     "subtipo": "CREDIT_CARD", "saldo": 0.0, "limite_disponivel": 0.01,
                     "fatura": None, "transacoes": []}]},
        {"item_id": "demo-3", "banco": "Itaú", "status": "LOGIN_ERROR", "atualizado": None,
         "erro": "Consentimento expirado — renove em meu.pluggy.ai", "contas": []},
    ]


# ----------------------------------------------------------------------------
# Cálculos
# ----------------------------------------------------------------------------

def data_brt(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(BRT).date()


def processar(conexoes, dias):
    hoje = datetime.now(BRT).date()
    inicio = hoje - timedelta(days=dias)
    ontem = hoje - timedelta(days=1)

    contas_banco, cartoes, futuras = [], [], []
    por_dia = defaultdict(lambda: {"entradas": 0.0, "saidas": 0.0})
    todas_saidas = []

    for con in conexoes:
        for c in con["contas"]:
            entradas = saidas = 0.0
            lista = []
            for t in c["transacoes"]:
                d = data_brt(t["data"])
                if d > hoje:  # agendamentos futuros
                    if c["tipo"] == "BANK":
                        futuras.append({**t, "dia": d, "banco": con["banco"], "conta": c["nome"]})
                    continue
                if d < inicio:
                    continue
                t = {**t, "dia": d, "banco": con["banco"], "conta": c["nome"]}
                lista.append(t)
                if t["tipo"] == "CREDIT":
                    entradas += t["valor"]
                else:
                    saidas += t["valor"]
                if c["tipo"] == "BANK":
                    k = "entradas" if t["tipo"] == "CREDIT" else "saidas"
                    por_dia[d][k] += t["valor"]
                    if t["tipo"] == "DEBIT":
                        todas_saidas.append(t)
            lista.sort(key=lambda x: x["data"], reverse=True)
            reg = {**c, "banco": con["banco"], "entradas": round(entradas, 2),
                   "saidas": round(saidas, 2), "lista": lista}
            if c["tipo"] != "BANK" and not c["saldo"] and not lista:
                continue  # cartão zerado e sem movimento (ex.: sem limite ativo)
            (contas_banco if c["tipo"] == "BANK" else cartoes).append(reg)

    dias_lista = [inicio + timedelta(days=i) for i in range(dias + 1)]
    resumo_dias = [{"dia": d, **{k: round(v, 2) for k, v in por_dia[d].items()}}
                   if d in por_dia else {"dia": d, "entradas": 0.0, "saidas": 0.0} for d in dias_lista]

    tot = {
        "saldo": round(sum(c["saldo"] for c in contas_banco), 2),
        "entradas": round(sum(c["entradas"] for c in contas_banco), 2),
        "saidas": round(sum(c["saidas"] for c in contas_banco), 2),
        "limite_uso": round(sum(c["saldo"] for c in cartoes), 2),
        "a_vencer": round(sum(c["fatura"]["valor"] for c in cartoes
                              if c.get("fatura") and data_brt(c["fatura"]["vencimento"]) >= hoje), 2),
        "gasto_cartao": round(sum(c["saidas"] for c in cartoes), 2),
    }
    tot["liquido"] = round(tot["entradas"] - tot["saidas"], 2)

    ontem_tx = sorted([t for c in contas_banco + cartoes for t in c["lista"] if t["dia"] == ontem],
                      key=lambda x: -x["valor"])
    maiores = sorted(todas_saidas + [t for c in cartoes for t in c["lista"] if t["tipo"] == "DEBIT"],
                     key=lambda x: -x["valor"])[:5]

    return {"hoje": hoje, "inicio": inicio, "ontem": ontem, "contas": contas_banco, "cartoes": cartoes,
            "dias": resumo_dias, "tot": tot, "ontem_tx": ontem_tx, "maiores": maiores,
            "futuras": sorted(futuras, key=lambda x: x["dia"])}


# ----------------------------------------------------------------------------
# Formatação
# ----------------------------------------------------------------------------

def brl(v):
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {s}" if v < 0 else f"R$ {s}"


def dm(d):
    return d.strftime("%d/%m")


DIAS_EXTRATO = 3  # extrato detalhado: últimos 3 dias (os totais seguem com 7)

DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

STATUS_OK = {"UPDATED"}
STATUS_TXT = {"UPDATED": "atualizado", "UPDATING": "atualizando", "LOGIN_ERROR": "erro de acesso",
              "OUTDATED": "desatualizado", "WAITING_USER_INPUT": "aguardando você"}

HORAS_SEM_SYNC = 36  # o Meu Pluggy sincroniza a cada 24h; acima disso, algo parou


def horas_sem_sync(c, agora=None):
    """Horas desde a última sincronização do banco, ou None se a data não veio."""
    if not c.get("atualizado"):
        return None
    try:
        d = datetime.fromisoformat(c["atualizado"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return ((agora or datetime.now(timezone.utc)) - d).total_seconds() / 3600


def problema_conexao(c):
    """Texto do problema da conexão, ou None se está tudo certo."""
    if c["erro"]:
        return c["erro"]
    if c["status"] not in STATUS_OK:
        return STATUS_TXT.get(c["status"], c["status"])
    h = horas_sem_sync(c)
    if h is not None and h > HORAS_SEM_SYNC:
        ult = datetime.fromisoformat(c["atualizado"].replace("Z", "+00:00")).astimezone(BRT)
        return (f"sem sincronizar há {int(h)} h (última em {ult.strftime('%d/%m %H:%M')}). "
                f"Os valores deste banco podem estar desatualizados; confira em meu.pluggy.ai")
    return None


def txt_fatura(c, hoje):
    f = c.get("fatura")
    if not f:
        return ""
    venc = data_brt(f["vencimento"])
    quando = f"vence {dm(venc)}" if venc >= hoje else f"venceu {dm(venc)}"
    return (f'<br><span style="font-size:11px;color:#6b6b6b">última fatura {brl(f["valor"])} · {quando}</span>')


def gerar_html(con, r):
    e = html.escape
    t = r["tot"]
    verde, vermelho, cinza = "#1b7f4b", "#b3261e", "#6b6b6b"

    def valor(v, tipo):
        cor = verde if tipo == "CREDIT" else vermelho
        sinal = "+" if tipo == "CREDIT" else "−"
        return f'<span style="color:{cor};white-space:nowrap">{sinal} {brl(v)}</span>'

    def linha_tx(x, mostrar_banco=True):
        pend = ' <span style="color:#a15c00;font-size:11px">(pendente)</span>' if x["status"] == "PENDING" else ""
        banco = f'<div style="color:{cinza};font-size:11px">{e(x["banco"])} · {e(x["conta"])}</div>' if mostrar_banco else ""
        return (f'<tr><td style="padding:6px 0;color:{cinza};width:48px;vertical-align:top">{dm(x["dia"])}</td>'
                f'<td style="padding:6px 8px">{e(x["descricao"])}{pend}{banco}</td>'
                f'<td style="padding:6px 0;text-align:right;vertical-align:top">{valor(x["valor"], x["tipo"])}</td></tr>')

    sec = []
    card = 'style="background:#fff;border:1px solid #e6e6e6;border-radius:10px;padding:16px;margin:0 0 14px"'
    h2 = 'style="font-size:15px;margin:0 0 10px;color:#222"'

    # Avisos de conexão
    avisos = [(c, problema_conexao(c)) for c in con]
    avisos = [(c, p) for c, p in avisos if p]
    if avisos:
        itens = "".join(f'<li><b>{e(c["banco"])}</b>: {e(p)}</li>' for c, p in avisos)
        sec.append(f'<div style="background:#fff4e5;border:1px solid #f0c27b;border-radius:10px;padding:12px 16px;margin:0 0 14px">'
                   f'<b>Atenção com conexões</b><ul style="margin:6px 0 0;padding-left:18px">{itens}</ul></div>')

    # Resumo
    liq_cor = verde if t["liquido"] >= 0 else vermelho
    sec.append(f'''<div {card}>
<div style="color:{cinza};font-size:12px">Saldo total em conta</div>
<div style="font-size:28px;font-weight:700;margin:2px 0 12px">{brl(t["saldo"])}</div>
<table width="100%" style="border-collapse:collapse;font-size:13px">
<tr><td style="color:{cinza}">Entradas ({dm(r["inicio"])}–{dm(r["hoje"])})</td><td style="text-align:right;color:{verde}">+ {brl(t["entradas"])}</td></tr>
<tr><td style="color:{cinza}">Saídas</td><td style="text-align:right;color:{vermelho}">− {brl(t["saidas"])}</td></tr>
<tr><td style="color:{cinza};border-top:1px solid #eee;padding-top:4px"><b>Resultado do período</b></td>
<td style="text-align:right;border-top:1px solid #eee;padding-top:4px;color:{liq_cor}"><b>{brl(t["liquido"])}</b></td></tr>
{f'<tr><td style="color:{cinza};padding-top:8px">Faturas fechadas a vencer</td><td style="text-align:right;padding-top:8px">{brl(t["a_vencer"])}</td></tr>' if r["cartoes"] else ""}
{f'<tr><td style="color:{cinza}">Limite de cartão em uso <span style="font-size:11px">(inclui parcelas futuras)</span></td><td style="text-align:right">{brl(t["limite_uso"])}</td></tr>' if r["cartoes"] else ""}
</table></div>''')

    # Por conta
    linhas = "".join(
        f'<tr><td style="padding:6px 0"><b>{e(c["banco"])}</b><div style="color:{cinza};font-size:11px">'
        f'{e(TIPOS_CONTA.get(c["subtipo"], c["nome"]))} {e(c["numero"])}</div></td>'
        f'<td style="text-align:right;font-size:12px"><span style="color:{verde}">+{brl(c["entradas"])}</span><br>'
        f'<span style="color:{vermelho}">−{brl(c["saidas"])}</span></td>'
        f'<td style="text-align:right;padding-left:10px"><b>{brl(c["saldo"])}</b></td></tr>'
        for c in r["contas"])
    for c in r["cartoes"]:
        lim = f'<br><span style="font-size:11px;color:{cinza}">limite livre {brl(float(c["limite_disponivel"]))}</span>' \
            if c["limite_disponivel"] else ""  # cartões sem limite pré-definido (ex.: Amex) vêm com 0
        linhas += (f'<tr><td style="padding:6px 0"><b>{e(c["banco"])}</b><div style="color:{cinza};font-size:11px">'
                   f'{e(c["nome"] if c["nome"].lower().startswith(("cartão", "cartao")) else "Cartão " + c["nome"])} {e(c["numero"])}</div></td>'
                   f'<td style="text-align:right;font-size:12px;color:{vermelho}">gastos 7d<br>−{brl(c["saidas"])}</td>'
                   f'<td style="text-align:right;padding-left:10px">limite em uso<br><b>{brl(c["saldo"])}</b>{lim}{txt_fatura(c, r["hoje"])}</td></tr>')
    sec.append(f'<div {card}><h2 {h2}>Por conta</h2>'
               f'<div style="color:{cinza};font-size:11px;margin:-6px 0 8px">Entradas e saídas de {dm(r["inicio"])} a {dm(r["hoje"])} · '
               f'à direita, saldo de hoje</div><table width="100%" style="border-collapse:collapse;font-size:13px">{linhas}</table></div>')

    # Dia a dia
    maxv = max([max(d["entradas"], d["saidas"]) for d in r["dias"]] + [1])
    dl = ""
    for d in r["dias"]:
        we = int(100 * d["entradas"] / maxv)
        ws = int(100 * d["saidas"] / maxv)
        dl += (f'<tr><td style="padding:4px 0;color:{cinza};width:70px">{DIAS_SEMANA[d["dia"].weekday()]} {dm(d["dia"])}</td>'
               f'<td style="padding:4px 8px"><div style="background:{verde};height:6px;width:{we}%;border-radius:3px;margin-bottom:2px"></div>'
               f'<div style="background:{vermelho};height:6px;width:{ws}%;border-radius:3px"></div></td>'
               f'<td style="text-align:right;font-size:12px;white-space:nowrap"><span style="color:{verde}">+{brl(d["entradas"])}</span><br>'
               f'<span style="color:{vermelho}">−{brl(d["saidas"])}</span></td></tr>')
    sec.append(f'<div {card}><h2 {h2}>Dia a dia (contas)</h2><table width="100%" style="border-collapse:collapse;font-size:13px">{dl}</table></div>')

    # Ontem
    corpo = "".join(linha_tx(x) for x in r["ontem_tx"]) or \
        f'<tr><td style="color:{cinza}">Nenhuma movimentação ontem.</td></tr>'
    sec.insert(1 if avisos else 0, f'<div {card}><h2 {h2}>Ontem ({dm(r["ontem"])})</h2><table width="100%" style="border-collapse:collapse;font-size:13px">{corpo}</table></div>')

    # Maiores saídas
    if r["maiores"]:
        sec.append(f'<div {card}><h2 {h2}>Maiores saídas da semana</h2><table width="100%" style="border-collapse:collapse;font-size:13px">'
                   + "".join(linha_tx(x) for x in r["maiores"]) + "</table></div>")

    # Agendados
    if r["futuras"]:
        sec.append(f'<div {card}><h2 {h2}>Agendados nos próximos dias</h2><table width="100%" style="border-collapse:collapse;font-size:13px">'
                   + "".join(linha_tx(x) for x in r["futuras"]) + "</table></div>")

    # Extrato completo
    corte = r["hoje"] - timedelta(days=DIAS_EXTRATO)
    for c in r["contas"] + r["cartoes"]:
        recentes = [x for x in c["lista"] if x["dia"] >= corte]
        if not recentes:
            continue
        sec.append(f'<div {card}><h2 {h2}>Extrato {dm(corte)}–{dm(r["hoje"])} — {e(c["banco"])} · {e(c["nome"])}</h2>'
                   f'<table width="100%" style="border-collapse:collapse;font-size:13px">'
                   + "".join(linha_tx(x, False) for x in recentes) + "</table></div>")

    # Rodapé
    atual = []
    for c in con:
        if c["atualizado"]:
            try:
                d = datetime.fromisoformat(c["atualizado"].replace("Z", "+00:00")).astimezone(BRT)
                atual.append(f'{e(c["banco"])} {d.strftime("%d/%m %H:%M")}')
            except ValueError:
                pass
    rod = (f'<p style="color:{cinza};font-size:11px;margin:16px 0 0">Dados sincronizados pelo Open Finance: '
           f'{" · ".join(atual) or "sem informação"}. Lançamentos podem levar algumas horas para aparecer. '
           f'Transferências entre suas próprias contas aparecem como saída em uma e entrada na outra.</p>')

    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f4f4f2;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222">
<div style="max-width:600px;margin:0 auto;padding:16px">
<div style="font-size:12px;color:{cinza}">Relatório bancário</div>
<h1 style="font-size:20px;margin:2px 0 14px">{DIAS_SEMANA[r["hoje"].weekday()]}, {r["hoje"].strftime("%d/%m/%Y")}</h1>
{"".join(sec)}{rod}</div></body></html>'''


def gerar_texto(r):
    t = r["tot"]
    linhas = [f"Relatório bancário — {r['hoje'].strftime('%d/%m/%Y')}",
              f"Saldo total em conta: {brl(t['saldo'])}",
              f"Entradas 7d: {brl(t['entradas'])} | Saídas 7d: {brl(t['saidas'])} | Resultado: {brl(t['liquido'])}"]
    for c in r["contas"]:
        linhas.append(f"- {c['banco']} {c['nome']}: {brl(c['saldo'])}")
    return "\n".join(linhas)


def resumo_log(con, r):
    """Linha para o log sem nenhum valor: o log do GitHub Actions não deve conter saldos."""
    erros = sum(1 for c in con if c["erro"])
    return (f"Relatório de {r['hoje'].strftime('%d/%m/%Y')} gerado: {len(con)} banco(s), "
            f"{len(r['contas'])} conta(s), {len(r['cartoes'])} cartão(ões), {erros} banco(s) com erro.")


def enviar_email(assunto, corpo_html, corpo_txt):
    remetente = os.environ["EMAIL_REMETENTE"]
    senha = os.environ["EMAIL_SENHA_APP"].replace(" ", "")
    destino = os.environ.get("EMAIL_DESTINO") or remetente
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = assunto, remetente, destino
    msg.attach(MIMEText(corpo_txt, "plain", "utf-8"))
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    porta = int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(host, porta, timeout=60) as smtp:
        smtp.login(remetente, senha)
        smtp.sendmail(remetente, [d.strip() for d in destino.split(",")], msg.as_string())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true", help="usa dados fictícios")
    p.add_argument("--no-send", action="store_true", help="não envia e-mail, só salva o HTML")
    a = p.parse_args()
    dias = int(os.environ.get("DIAS", "7"))

    con = coletar_demo(dias) if a.demo else coletar_pluggy(dias)
    r = processar(con, dias)
    corpo = gerar_html(con, r)
    arquivo = "relatorio_demo.html" if a.demo else "relatorio.html"
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write(corpo)
    # Valores só aparecem no terminal quando rodando no próprio computador (--demo / --no-send)
    print(gerar_texto(r) if (a.demo or a.no_send) else resumo_log(con, r))

    if not a.demo and not a.no_send:
        assunto = f"Extrato Bancos {r['hoje'].strftime('%d/%m/%Y')}"
        if any(problema_conexao(c) for c in con):
            assunto = "⚠ " + assunto
        enviar_email(assunto, corpo, gerar_texto(r))
        print("E-mail enviado.")

    if not a.demo and all(c["erro"] for c in con):
        sys.exit("Nenhum banco respondeu — verifique as credenciais.")


if __name__ == "__main__":
    main()
