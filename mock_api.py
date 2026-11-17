#!/usr/bin/env python3
"""
API de exemplo para testar a coleta sem credencial nem dado real.

Reproduz os defeitos que motivaram a coleta adaptativa:
  - página limitada a 50 registros, mesmo pedindo mais;
  - offset fundo instável: a partir de --offset-instavel a página vem deslocada,
    então a paginação ingênua duplica alguns registros e pula outros;
  - falhas aleatórias 429/503 (o cliente precisa tentar de novo);
  - login por formulário que devolve cookie de sessão.

Os dados são fictícios e gerados de forma determinística (mesma --seed, mesmos dados).

Uso:
  python mock_api.py                       # http://127.0.0.1:8765, 10 dias a partir de 2026-10-01
  python mock_api.py --dias 3 --por-dia 500
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LIMITE_PAGINA = 50
USUARIO = ("demo@exemplo.com", "demo")
TOKEN_FIXO = "demo-token"

NOMES = ["Ana", "Bruno", "Carla", "Diego", "Elisa", "Fábio", "Gabriela", "Hugo", "Isabela", "João",
         "Larissa", "Marcos", "Natália", "Otávio", "Paula", "Rafael", "Sabrina", "Tiago", "Vanessa", "Yuri"]
SOBRENOMES = ["Almeida", "Barbosa", "Cardoso", "Dias", "Ferreira", "Gomes", "Lima", "Martins",
              "Nunes", "Oliveira", "Pereira", "Ribeiro", "Santos", "Souza", "Teixeira"]

# Notificações de status de pedido. O nome do template muda no meio do período
# (v1 -> nomes por situação), como acontece em produção quando alguém renomeia.
SITUACOES = [("aprovado", 0.82), ("recusado", 0.05), ("em_analise", 0.13)]
OUTROS_TEMPLATES = [("lembrete_pagamento", 0.04), ("pesquisa_satisfacao", 0.03)]
STATUS_ENVIO = [("read", 0.55), ("delivered", 0.37), ("sent", 0.05), ("failed", 0.03)]
TEXTO = {
    "aprovado": "Olá, {nome}! O pedido {pedido} foi aprovado. Você já pode seguir com o atendimento.",
    "recusado": "Olá, {nome}. O pedido {pedido} não foi aprovado. Fale com a central para entender o motivo.",
    "em_analise": "Olá, {nome}. O pedido {pedido} foi recebido e está em análise. Avisaremos quando houver resposta.",
    "lembrete_pagamento": "Olá, {nome}. Lembrete: há um pagamento em aberto. Responda esta mensagem se precisar de ajuda.",
    "pesquisa_satisfacao": "Olá, {nome}! Como foi o seu atendimento? Responda com uma nota de 1 a 5.",
}


def _sorteio(rng: random.Random, opcoes):
    valores, pesos = zip(*opcoes)
    return rng.choices(valores, weights=pesos, k=1)[0]


def _esquema_pesado(rng: random.Random) -> dict:
    """Simula o 'schema' que a API devolve dentro de cada contato: pesado e inútil para o relatório."""
    return {f"campo_{i}": {"tipo": "texto", "obrigatorio": bool(i % 2), "opcoes": list(range(rng.randint(5, 15)))}
            for i in range(30)}


def gerar_registros(inicio: datetime, dias: int, por_dia: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    meio = inicio + timedelta(days=dias / 2)
    registros = []
    for d in range(dias):
        dia = inicio + timedelta(days=d)
        fim_de_semana = dia.weekday() >= 5
        qtd = int(por_dia * (0.25 if fim_de_semana else rng.uniform(0.85, 1.15)))
        for _ in range(qtd):
            # concentra o volume no horário comercial (08h–18h no horário de Brasília = 11h–21h UTC)
            if rng.random() < 0.9:
                seg = rng.randint(11 * 3600, 21 * 3600 - 1)
            else:
                seg = rng.randint(0, 24 * 3600 - 1)
            criado = dia + timedelta(seconds=seg)
            nome = f"{rng.choice(NOMES)} {rng.choice(SOBRENOMES)}"
            if rng.random() < sum(p for _, p in OUTROS_TEMPLATES):
                template, situacao = _sorteio(rng, OUTROS_TEMPLATES), None
            else:
                situacao = _sorteio(rng, SITUACOES)
                template = "status_pedido_v1" if criado < meio else f"status_pedido_{situacao}"
            registros.append({
                "id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                "created_at": criado.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type": "outbound",
                "channel": "whatsapp",
                "template_name": template,
                "status": _sorteio(rng, STATUS_ENVIO),
                "contact": {
                    "name": nome,
                    "phone": f"55{rng.randint(11, 99)}9{rng.randint(10000000, 99999999)}",
                    "schema": _esquema_pesado(rng),
                },
                "fields": {
                    "pedido": f"PED-{rng.randint(100000, 999999)}",
                    "situacao": situacao or "",
                },
            })
    registros.sort(key=lambda r: (r["created_at"], r["id"]))
    return registros


class Base:
    """Estado compartilhado entre as requisições."""

    def __init__(self, registros, falhas: float, offset_instavel: int, seed: int):
        self.registros = registros
        self.horarios = [_data(r["created_at"]) for r in registros]
        self.por_id = {r["id"]: r for r in registros}
        self.falhas = falhas
        self.offset_instavel = offset_instavel
        self.sessoes: set[str] = set()
        self.rng = random.Random(seed + 1)
        self.trava = threading.Lock()

    def sortear_falha(self) -> int | None:
        with self.trava:
            if self.rng.random() < self.falhas:
                return self.rng.choice([429, 503])
        return None


def _data(texto: str) -> datetime:
    d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _offset_efetivo(offset: int, limite_instavel: int) -> int:
    """Abaixo do limite, a página é a pedida. Acima, vem deslocada de -5 a +5 posições
    (de forma determinística), o que gera duplicados e buracos na paginação ingênua."""
    if offset < limite_instavel:
        return offset
    return max(0, offset + ((offset * 7919) % 11) - 5)


def criar_handler(base: Base):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silencioso
            pass

        def _responder(self, status: int, corpo: dict | None = None, cabecalhos: dict | None = None):
            dados = json.dumps(corpo or {}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(dados)))
            for k, v in (cabecalhos or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(dados)

        def _autenticado(self) -> bool:
            if self.headers.get("Authorization", "") == f"Bearer {TOKEN_FIXO}":
                return True
            for parte in self.headers.get("Cookie", "").split(";"):
                nome, _, valor = parte.strip().partition("=")
                if nome == "sessao" and valor in base.sessoes:
                    return True
            return False

        def do_POST(self):
            if urlparse(self.path).path != "/api/v1/login":
                return self._responder(404, {"erro": "não encontrado"})
            tamanho = int(self.headers.get("Content-Length") or 0)
            bruto = self.rfile.read(tamanho).decode("utf-8")
            if "json" in self.headers.get("Content-Type", ""):
                form = json.loads(bruto or "{}")
            else:
                form = {k: v[0] for k, v in parse_qs(bruto).items()}
            if (form.get("email"), form.get("password")) != USUARIO:
                return self._responder(401, {"erro": "credenciais inválidas"})
            sessao = secrets.token_hex(16)
            base.sessoes.add(sessao)
            return self._responder(200, {"ok": True}, {"Set-Cookie": f"sessao={sessao}; Path=/; HttpOnly"})

        def do_GET(self):
            url = urlparse(self.path)
            if not self._autenticado():
                return self._responder(401, {"erro": "sessão inválida"})
            falha = base.sortear_falha()
            if falha:
                return self._responder(falha, {"erro": "tente de novo"}, {"Retry-After": "0"})

            partes = url.path.strip("/").split("/")
            if url.path == "/api/v1/records":
                return self._listar(parse_qs(url.query))
            if len(partes) == 5 and partes[:3] == ["api", "v1", "records"] and partes[4] == "messages":
                return self._mensagens(partes[3])
            return self._responder(404, {"erro": "não encontrado"})

        def _listar(self, q):
            try:
                inicio, fim = _data(q["start"][0]), _data(q["end"][0])
            except (KeyError, ValueError):
                return self._responder(400, {"erro": "start e end (ISO 8601) são obrigatórios"})
            limite = min(int(q.get("_limit", [LIMITE_PAGINA])[0]), LIMITE_PAGINA)
            offset = int(q.get("_offset", [0])[0])
            # janela [start, end): inclui o início e exclui o fim
            a = bisect.bisect_left(base.horarios, inicio)
            b = bisect.bisect_left(base.horarios, fim)
            janela = base.registros[a:b]
            o = _offset_efetivo(offset, base.offset_instavel)
            pagina = janela[o:o + limite]
            return self._responder(200, {"data": pagina, "pagination": {"total": len(janela),
                                                                         "limit": limite, "offset": offset}})

        def _mensagens(self, rid):
            r = base.por_id.get(rid)
            if not r:
                return self._responder(404, {"erro": "registro não encontrado"})
            chave = r["fields"]["situacao"] or r["template_name"]
            texto = TEXTO[chave].format(nome=r["contact"]["name"].split()[0], pedido=r["fields"]["pedido"])
            return self._responder(200, {"data": {"messages": [
                {"author": "SYSTEM", "message": texto, "created_at": r["created_at"]},
            ]}})

    return Handler


def criar_servidor(host="127.0.0.1", porta=8765, inicio="2026-10-01", dias=10, por_dia=2000,
                   falhas=0.03, offset_instavel=1000, seed=42) -> ThreadingHTTPServer:
    t0 = datetime.fromisoformat(inicio).replace(tzinfo=timezone.utc) + timedelta(hours=3)  # 00h de Brasília
    base = Base(gerar_registros(t0, dias, por_dia, seed), falhas, offset_instavel, seed)
    servidor = ThreadingHTTPServer((host, porta), criar_handler(base))
    servidor.base = base
    return servidor


def main():
    ap = argparse.ArgumentParser(description="API de exemplo para a coleta adaptativa")
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--inicio", default="2026-10-01", help="primeiro dia dos dados (AAAA-MM-DD)")
    ap.add_argument("--dias", type=int, default=10)
    ap.add_argument("--por-dia", type=int, default=2000, help="registros por dia útil (fim de semana tem 25%%)")
    ap.add_argument("--falhas", type=float, default=0.03, help="fração de requisições que falham com 429/503")
    ap.add_argument("--offset-instavel", type=int, default=1000, help="a partir de qual offset a paginação desanda")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    srv = criar_servidor(porta=a.porta, inicio=a.inicio, dias=a.dias, por_dia=a.por_dia,
                         falhas=a.falhas, offset_instavel=a.offset_instavel, seed=a.seed)
    print(f"API de exemplo em http://127.0.0.1:{a.porta} · {len(srv.base.registros):,} registros · "
          f"login {USUARIO[0]} / {USUARIO[1]} (ou Bearer {TOKEN_FIXO})".replace(",", "."))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
