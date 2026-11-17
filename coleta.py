#!/usr/bin/env python3
"""
Coleta adaptativa de APIs paginadas.

Extrai grandes volumes de uma API REST que pagina por _limit/_offset e que perde ou
duplica registros quando o offset fica fundo. Em vez de paginar fundo, a coleta divide
o período em janelas de tempo cada vez menores, até cada janela caber numa paginação
rasa e confiável.

Subcomandos:
  coletar     coleta os registros do período (janela adaptativa, retomável por dia)
  templates   lista os templates encontrados no período, com contagem
  enriquecer  busca o texto de cada mensagem em paralelo (cache, retomável)
  gerar       gera o relatório XLSX (registros + resumo)
  run         coletar + enriquecer + gerar
  comparar    pagina do jeito ingênuo e do jeito adaptativo e compara os totais

Configuração por variáveis de ambiente ou arquivo .env (ver .env.example).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    sys.exit("Falta a biblioteca 'requests'. Rode: pip install -r requirements.txt")


# --------------------------------------------------------------------------- configuração
def _carregar_env(caminho: Path) -> None:
    """Lê um .env simples (CHAVE=valor). Variáveis já definidas no ambiente têm prioridade."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.strip()
        if valor[:1] in ('"', "'"):
            valor = valor[1:].split(valor[0], 1)[0]
        elif valor.startswith("#"):                              # CHAVE=   # comentário -> vazio
            valor = ""
        else:
            valor = valor.split(" #", 1)[0].strip()             # comentário no fim da linha
        os.environ.setdefault(chave.strip(), valor)


_carregar_env(Path(__file__).with_name(".env"))


def _env(nome: str, padrao: str | None = None) -> str | None:
    valor = os.environ.get(nome)
    return valor if valor not in (None, "") else padrao


API_URL = _env("API_URL", "http://127.0.0.1:8765").rstrip("/")
ENDPOINT_LOGIN = _env("ENDPOINT_LOGIN", "/api/v1/login")
ENDPOINT_LISTA = _env("ENDPOINT_LISTA", "/api/v1/records")
ENDPOINT_DETALHE = _env("ENDPOINT_DETALHE", "/api/v1/records/{id}/messages")
API_TOKEN = _env("API_TOKEN")
API_EMAIL = _env("API_EMAIL")
API_SENHA = _env("API_SENHA")

TIMEZONE = ZoneInfo(_env("TIMEZONE", "America/Sao_Paulo"))
PAGE_LIMIT = int(_env("PAGE_LIMIT", "50"))            # teto real da API: pedir mais devolve 50 do mesmo jeito
JANELA_SEGURA = int(_env("JANELA_SEGURA", "1000"))    # total máximo que ainda pagina sem dividir
JANELA_MIN_SEG = int(_env("JANELA_MIN_SEG", "60"))    # não divide janelas menores que isso
TEMPLATES_PREFIXOS = [p.strip() for p in (_env("TEMPLATES_PREFIXOS", "") or "").split(",") if p.strip()]
DESCARTAR_CAMPOS = [c.strip() for c in (_env("DESCARTAR_CAMPOS", "contact.schema") or "").split(",") if c.strip()]
WORKERS = int(_env("WORKERS", "12"))
TIMEOUT = int(_env("TIMEOUT", "60"))
MAX_TENTATIVAS = int(_env("MAX_TENTATIVAS", "5"))
BACKOFF_BASE = float(_env("BACKOFF_BASE", "1"))
DIR_SAIDA = Path(_env("DIR_SAIDA", str(Path(__file__).with_name("saida"))))

RETENTAVEIS = {429, 500, 502, 503, 504}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


# --------------------------------------------------------------------------- período
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _meia_noite(d: date) -> datetime:
    return datetime.combine(d, dtime.min, TIMEZONE).astimezone(timezone.utc)


def montar_periodo(args) -> tuple[datetime, datetime, str]:
    """Devolve (início, fim) em UTC e um sufixo para os arquivos. O fim é exclusivo."""
    if getattr(args, "mes_passado", False):
        hoje = datetime.now(TIMEZONE).date()
        fim = hoje.replace(day=1)
        inicio = (fim - timedelta(days=1)).replace(day=1)
        return _meia_noite(inicio), _meia_noite(fim), f"{inicio:%Y-%m}"
    if not (args.inicio and args.fim):
        sys.exit("Informe --inicio e --fim (AAAA-MM-DD) ou --mes-passado.")
    inicio = date.fromisoformat(args.inicio)
    fim = date.fromisoformat(args.fim)  # inclusivo para quem digita
    if fim < inicio:
        sys.exit("--fim é anterior a --inicio.")
    return _meia_noite(inicio), _meia_noite(fim + timedelta(days=1)), f"{inicio:%Y%m%d}_{fim:%Y%m%d}"


def dias_do_periodo(inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
    """Fatia o período em dias do fuso local (cada dia é um checkpoint da coleta)."""
    dias, d = [], inicio.astimezone(TIMEZONE).date()
    while _meia_noite(d) < fim:
        dias.append((max(_meia_noite(d), inicio), min(_meia_noite(d + timedelta(days=1)), fim)))
        d += timedelta(days=1)
    return dias


# --------------------------------------------------------------------------- HTTP
class SessaoExpirada(Exception):
    pass


def autenticar() -> requests.Session:
    """Token fixo (Bearer) ou login programático com e-mail e senha.

    O login programático substitui o cookie colado à mão, que expirava no meio da coleta."""
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    adaptador = HTTPAdapter(pool_connections=WORKERS * 2, pool_maxsize=WORKERS * 2)
    s.mount("http://", adaptador)
    s.mount("https://", adaptador)

    if API_TOKEN:
        s.headers["Authorization"] = f"Bearer {API_TOKEN}"
        return s
    if API_EMAIL and API_SENHA:
        for tentativa in range(1, 4):
            try:
                r = s.post(API_URL + ENDPOINT_LOGIN, data={"email": API_EMAIL, "password": API_SENHA},
                           timeout=TIMEOUT)
                if r.ok:
                    log(f"Login OK ({API_EMAIL}).")
                    return s
                log(f"Login devolveu {r.status_code} (tentativa {tentativa}/3).")
            except requests.RequestException as e:
                log(f"Falha no login (tentativa {tentativa}/3): {e}")
            time.sleep(BACKOFF_BASE * 2 ** tentativa)
        sys.exit("Não consegui fazer login. Confira API_EMAIL e API_SENHA.")
    sys.exit("Sem credencial: defina API_TOKEN ou API_EMAIL + API_SENHA.")


def _espera(tentativa: int, resposta: requests.Response | None = None) -> float:
    """Backoff exponencial com jitter; respeita Retry-After quando a API manda."""
    if resposta is not None and resposta.headers.get("Retry-After", "").isdigit():
        return float(resposta.headers["Retry-After"])
    return BACKOFF_BASE * (2 ** (tentativa - 1)) + random.uniform(0, BACKOFF_BASE)


def get_json(s: requests.Session, url: str, params: dict | None = None) -> dict:
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            r = s.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            if tentativa == MAX_TENTATIVAS:
                raise
            time.sleep(_espera(tentativa))
            continue
        if r.status_code == 401:
            raise SessaoExpirada(url)
        if r.status_code in RETENTAVEIS and tentativa < MAX_TENTATIVAS:
            time.sleep(_espera(tentativa, r))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("inalcançável")


# --------------------------------------------------------------------------- adaptadores
# Os três pontos abaixo são os únicos que dependem do formato da API. Para usar com
# outra API, ajuste estas funções e as variáveis ENDPOINT_* do .env.
def buscar_pagina(s: requests.Session, inicio: datetime, fim: datetime, offset: int) -> tuple[list, int | None]:
    corpo = get_json(s, API_URL + ENDPOINT_LISTA, params={
        "start": iso(inicio), "end": iso(fim), "_limit": PAGE_LIMIT, "_offset": offset,
    })
    return corpo.get("data") or [], (corpo.get("pagination") or {}).get("total")


def extrair_mensagem(corpo: dict) -> str:
    """Do detalhe da conversa, pega o texto que o sistema enviou (autor SYSTEM)."""
    mensagens = (corpo.get("data") or {}).get("messages") or []
    for m in mensagens:
        if m.get("author") == "SYSTEM" and (m.get("message") or "").strip():
            return m["message"]
    return ""


def montar_linha(r: dict, mensagem: str | None) -> dict:
    contato, campos = r.get("contact") or {}, r.get("fields") or {}
    criado = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(TIMEZONE)
    return {
        "id": r.get("id"),
        "data_hora": criado.strftime("%d/%m/%Y %H:%M:%S"),
        "nome": contato.get("name"),
        "telefone": contato.get("phone"),
        "template": r.get("template_name"),
        "status_envio": r.get("status"),
        "pedido": campos.get("pedido"),
        "situacao": campos.get("situacao"),
        "mensagem": " ".join((mensagem or "").split()) if mensagem is not None else "(não buscada)",
    }


# --------------------------------------------------------------------------- acumulador
def enxugar(r: dict) -> dict:
    """Remove campos pesados que o relatório não usa (ex.: 'contact.schema').

    Em produção, isso derrubou o pico de memória de 14,8 GB para ~0,5 GB num mês de coleta."""
    for caminho in DESCARTAR_CAMPOS:
        *pais, chave = caminho.split(".")
        alvo = r
        for p in pais:
            alvo = alvo.get(p) if isinstance(alvo, dict) else None
        if isinstance(alvo, dict):
            alvo.pop(chave, None)
    return r


class Acumulador:
    """Guarda registros únicos por id. A deduplicação é a rede de segurança: com janelas
    rasas ela não deveria encontrar nada, e contar duplicados mostra quando a API desanda."""

    def __init__(self, itens=()):
        self.itens = list(itens)
        self.vistos = {r.get("id") for r in self.itens}
        self.duplicados = 0

    def absorver(self, pagina: list) -> int:
        novos = 0
        for r in pagina:
            rid = r.get("id")
            if rid in self.vistos:
                self.duplicados += 1
                continue
            self.vistos.add(rid)
            self.itens.append(enxugar(r))
            novos += 1
        return novos


# --------------------------------------------------------------------------- coleta
def coletar_janela(s: requests.Session, inicio: datetime, fim: datetime, acc: Acumulador, nivel: int = 0) -> None:
    """Coleta recursiva por janela de tempo.

    1. Pede a 1ª página da janela e lê o total.
    2. Total acima de JANELA_SEGURA e janela ainda divisível: divide ao meio e repete em cada metade.
    3. Senão: pagina raso até o fim (offset nunca passa de JANELA_SEGURA).
    """
    recuo = "  " * nivel
    rotulo = f"{inicio.astimezone(TIMEZONE):%d/%m %H:%M:%S} → {fim.astimezone(TIMEZONE):%d/%m %H:%M:%S}"
    pagina, total = buscar_pagina(s, inicio, fim, 0)
    total = len(pagina) if total is None else total
    duracao = int((fim - inicio).total_seconds())

    if total > JANELA_SEGURA and duracao > JANELA_MIN_SEG:
        meio = inicio + timedelta(seconds=duracao // 2)
        log(f"{recuo}{rotulo} · total {_num(total)} > {_num(JANELA_SEGURA)} → dividindo")
        coletar_janela(s, inicio, meio, acc, nivel + 1)
        coletar_janela(s, meio, fim, acc, nivel + 1)
        return
    if total > JANELA_SEGURA:
        log(f"{recuo}⚠️ {rotulo} · janela mínima com {_num(total)} registros; paginando fundo mesmo assim")

    novos = acc.absorver(pagina)
    offset = PAGE_LIMIT
    while offset < total and len(pagina) == PAGE_LIMIT:
        pagina, _ = buscar_pagina(s, inicio, fim, offset)
        if not pagina:
            break
        novos += acc.absorver(pagina)
        offset += PAGE_LIMIT
    log(f"{recuo}{rotulo} · total {_num(total)} · +{_num(novos)} novos · acumulado {_num(len(acc.itens))}")


def salvar_json_lista(caminho: Path, itens: list) -> None:
    """Grava registro a registro num .tmp e só então troca pelo definitivo.

    Não monta a string inteira na memória e, se o processo morrer no meio,
    o arquivo anterior continua íntegro (em produção, um checkpoint corrompido
    já tinha deixado o arquivo com 0 bytes)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("[")
        for i, item in enumerate(itens):
            f.write(",\n" if i else "\n")
            f.write(json.dumps(item, ensure_ascii=False))
        f.write("\n]")
    os.replace(tmp, caminho)


def _caminho(prefixo: str, sufixo: str) -> Path:
    return DIR_SAIDA / f"{prefixo}_{sufixo}.json"


def cmd_coletar(args, s: requests.Session | None = None) -> tuple[str, list]:
    inicio, fim, sufixo = montar_periodo(args)
    log(f"Período {iso(inicio)} → {iso(fim)} (arquivos com sufixo {sufixo})")
    bruto, feitos_path = _caminho("bruto", sufixo), _caminho("bruto", sufixo + ".dias")

    itens, dias_feitos = [], set()
    if bruto.exists() and feitos_path.exists():
        itens = json.loads(bruto.read_text(encoding="utf-8"))
        dias_feitos = set(json.loads(feitos_path.read_text(encoding="utf-8")))
        log(f"Retomando: {_num(len(itens))} registros e {len(dias_feitos)} dia(s) já coletados")
    elif bruto.exists():
        log("Coleta deste período já concluída. Apague o arquivo bruto para coletar de novo.")
        return sufixo, json.loads(bruto.read_text(encoding="utf-8"))

    acc = Acumulador(itens)
    s = s or autenticar()
    dias = dias_do_periodo(inicio, fim)
    t0 = time.time()
    for i, (d0, d1) in enumerate(dias, 1):
        chave = iso(d0)
        if chave in dias_feitos:
            continue
        log(f"[{i}/{len(dias)}] {d0.astimezone(TIMEZONE):%d/%m/%Y}")
        antes = len(acc.itens)
        coletar_janela(s, d0, d1, acc, nivel=1)
        dias_feitos.add(chave)
        salvar_json_lista(bruto, acc.itens)                     # checkpoint ao fim de cada dia
        feitos_path.write_text(json.dumps(sorted(dias_feitos)), encoding="utf-8")
        log(f"   dia concluído: +{_num(len(acc.itens) - antes)} · acumulado {_num(len(acc.itens))}")

    feitos_path.unlink(missing_ok=True)                         # coleta completa: some o checkpoint
    log(f"Coleta concluída: {_num(len(acc.itens))} registros únicos em {time.time() - t0:.0f}s "
        f"({acc.duplicados} duplicados descartados) → {bruto}")
    return sufixo, acc.itens


def _carregar_bruto(sufixo: str) -> list:
    caminho = _caminho("bruto", sufixo)
    if not caminho.exists():
        sys.exit(f"Arquivo não encontrado: {caminho}. Rode 'coletar' primeiro.")
    return json.loads(caminho.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- filtro de template
def eh_template_alvo(nome: str | None) -> bool:
    """Filtro por prefixo, não por nome exato: absorve renomeações do template
    (ex.: status_pedido_v1 → status_pedido_aprovado) sem mudar código."""
    if not TEMPLATES_PREFIXOS:
        return True
    return bool(nome) and any(nome.startswith(p) for p in TEMPLATES_PREFIXOS)


def eh_alvo(r: dict) -> bool:
    return r.get("type") == "outbound" and eh_template_alvo(r.get("template_name"))


def cmd_templates(args) -> None:
    _, _, sufixo = montar_periodo(args)
    contagem = Counter(r.get("template_name") or "(vazio)" for r in _carregar_bruto(sufixo)
                       if r.get("type") == "outbound")
    filtro = ", ".join(TEMPLATES_PREFIXOS) or "(sem filtro: todos entram)"
    log(f"Templates no período · filtro atual: {filtro}")
    for nome, qtd in contagem.most_common():
        marca = "  ← entra" if TEMPLATES_PREFIXOS and eh_template_alvo(nome) else ""
        print(f"   {_num(qtd):>9}  {nome}{marca}")


# --------------------------------------------------------------------------- enriquecimento
def buscar_mensagem(s: requests.Session, rid: str) -> str | None:
    """Texto real da mensagem. None em falha, para ser tentado de novo na próxima rodada."""
    try:
        return extrair_mensagem(get_json(s, API_URL + ENDPOINT_DETALHE.format(id=rid)))
    except (SessaoExpirada, requests.RequestException):
        return None


def cmd_enriquecer(args, s: requests.Session | None = None) -> tuple[str, dict]:
    _, _, sufixo = montar_periodo(args)
    ids = list(dict.fromkeys(r["id"] for r in _carregar_bruto(sufixo) if eh_alvo(r)))
    cache_path = _caminho("mensagens", sufixo)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    pendentes = [i for i in ids if i not in cache]
    log(f"{_num(len(ids))} mensagens no filtro · {_num(len(cache))} em cache · {_num(len(pendentes))} a buscar")
    if not pendentes:
        return sufixo, cache

    s = s or autenticar()
    feitos = falhas = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futuros = {ex.submit(buscar_mensagem, s, rid): rid for rid in pendentes}
        for fut in as_completed(futuros):
            texto = fut.result()
            if texto is None:
                falhas += 1                       # não entra no cache: a próxima rodada tenta de novo
            else:
                cache[futuros[fut]] = texto
            feitos += 1
            if feitos % 2000 == 0:
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                log(f"   {_num(feitos)}/{_num(len(pendentes))} · {feitos / max(time.time() - t0, 1e-6):.0f}/s")
    DIR_SAIDA.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    log(f"Enriquecimento: {_num(len(cache))} em cache · {falhas} falha(s) nesta rodada · {time.time() - t0:.0f}s")
    if falhas:
        log("   Rode 'enriquecer' de novo: ele busca só o que faltou.")
    return sufixo, cache


# --------------------------------------------------------------------------- relatório
def cmd_gerar(args) -> Path:
    import pandas as pd

    _, _, sufixo = montar_periodo(args)
    bruto = _carregar_bruto(sufixo)
    cache_path = _caminho("mensagens", sufixo)
    mensagens = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    alvo = [r for r in bruto if eh_alvo(r)]
    df = pd.DataFrame([montar_linha(r, mensagens.get(r["id"])) for r in alvo])
    if df.empty:
        sys.exit("Nenhum registro passou no filtro. Rode 'templates' para ver os nomes do período.")

    entregues = df["status_envio"].isin(["delivered", "read"])
    resumo = pd.DataFrame([
        ("Registros coletados", len(bruto)),
        ("Registros no filtro de template", len(df)),
        ("Entregues ou lidos", int(entregues.sum())),
        ("Taxa de entrega", f"{entregues.mean() * 100:.2f}%".replace(".", ",")),
        ("Falhas de envio", int((df["status_envio"] == "failed").sum())),
        ("Mensagens com texto real", int((df["mensagem"] != "(não buscada)").sum())),
    ], columns=["Indicador", "Valor"])
    por_template = (df.pivot_table(index="template", columns="status_envio", values="id",
                                   aggfunc="count", fill_value=0)
                      .assign(total=lambda t: t.sum(axis=1))
                      .sort_values("total", ascending=False)
                      .reset_index())

    saida = DIR_SAIDA / f"relatorio_{sufixo}.xlsx"
    DIR_SAIDA.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(saida, engine="openpyxl") as xls:
        resumo.to_excel(xls, sheet_name="Resumo", index=False)
        por_template.to_excel(xls, sheet_name="Por template", index=False)
        df.to_excel(xls, sheet_name="Registros", index=False)
        for aba in xls.sheets.values():
            for coluna in aba.columns:
                largura = max(len(str(c.value or "")) for c in coluna[:200])
                aba.column_dimensions[coluna[0].column_letter].width = min(max(largura + 2, 10), 60)
            aba.freeze_panes = "A2"
    log(f"Relatório: {_num(len(df))} linhas → {saida}")
    return saida


def cmd_run(args) -> None:
    s = autenticar()
    cmd_coletar(args, s)
    cmd_enriquecer(args, s)
    cmd_gerar(args)


# --------------------------------------------------------------------------- comparação
def coletar_ingenuo(s: requests.Session, inicio: datetime, fim: datetime) -> tuple[Acumulador, int]:
    """Do jeito que costuma ser feito: uma consulta para o período todo, offset até o fim."""
    acc = Acumulador()
    pagina, total = buscar_pagina(s, inicio, fim, 0)
    acc.absorver(pagina)
    for offset in range(PAGE_LIMIT, total or 0, PAGE_LIMIT):
        pagina, _ = buscar_pagina(s, inicio, fim, offset)
        acc.absorver(pagina)
    return acc, total or 0


def cmd_comparar(args) -> dict:
    inicio, fim, _ = montar_periodo(args)
    s = autenticar()
    log("Paginação ingênua (offset fundo)…")
    t0 = time.time()
    ingenuo, total = coletar_ingenuo(s, inicio, fim)
    t_ingenuo = time.time() - t0
    log("Coleta adaptativa (janelas)…")
    t0 = time.time()
    adaptativo = Acumulador()
    for d0, d1 in dias_do_periodo(inicio, fim):
        coletar_janela(s, d0, d1, adaptativo, nivel=1)
    t_adaptativo = time.time() - t0

    def faltando(acc):
        return total - len(acc.itens)

    print()
    print(f"Total informado pela API ....... {_num(total):>9}")
    print(f"Ingênuo (offset fundo) ......... {_num(len(ingenuo.itens)):>9} únicos · "
          f"{_num(ingenuo.duplicados)} duplicados · {_num(faltando(ingenuo))} faltando · {t_ingenuo:.1f}s")
    print(f"Adaptativo (janelas) ........... {_num(len(adaptativo.itens)):>9} únicos · "
          f"{_num(adaptativo.duplicados)} duplicados · {_num(faltando(adaptativo))} faltando · {t_adaptativo:.1f}s")
    return {"total": total, "ingenuo": len(ingenuo.itens), "ingenuo_duplicados": ingenuo.duplicados,
            "adaptativo": len(adaptativo.itens), "adaptativo_duplicados": adaptativo.duplicados}


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Coleta adaptativa de APIs paginadas")
    sub = ap.add_subparsers(dest="cmd", required=True)
    comandos = {
        "coletar": cmd_coletar, "templates": cmd_templates, "enriquecer": cmd_enriquecer,
        "gerar": cmd_gerar, "run": cmd_run, "comparar": cmd_comparar,
    }
    for nome in comandos:
        p = sub.add_parser(nome)
        p.add_argument("--inicio", help="primeiro dia (AAAA-MM-DD)")
        p.add_argument("--fim", help="último dia, inclusivo (AAAA-MM-DD)")
        p.add_argument("--mes-passado", action="store_true", help="período = mês anterior completo")
    args = ap.parse_args(argv)
    try:
        comandos[args.cmd](args)
    except SessaoExpirada:
        sys.exit("Sessão expirada (401). Rode de novo: a coleta retoma do último dia concluído.")


if __name__ == "__main__":
    main()
