"""Testes ponta a ponta contra a API de exemplo (sobe um servidor local numa porta livre)."""
import json
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import coleta  # noqa: E402
import mock_api  # noqa: E402

PERIODO = Namespace(inicio="2026-10-01", fim="2026-10-02", mes_passado=False)


class _Interrompido(Exception):
    pass


def setUpModule():
    global servidor, sessao, tmp
    servidor = mock_api.criar_servidor(porta=0, dias=2, por_dia=1500, falhas=0.05, offset_instavel=1000, seed=7)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    tmp = tempfile.TemporaryDirectory()
    coleta.API_URL = f"http://127.0.0.1:{servidor.server_address[1]}"
    coleta.API_TOKEN = mock_api.TOKEN_FIXO
    coleta.JANELA_SEGURA = 300
    coleta.BACKOFF_BASE = 0.001
    coleta.TEMPLATES_PREFIXOS = ["status_pedido_"]
    coleta.DIR_SAIDA = Path(tmp.name)
    sessao = coleta.autenticar()


def tearDownModule():
    servidor.shutdown()
    tmp.cleanup()


class TestColeta(unittest.TestCase):
    def setUp(self):
        for f in coleta.DIR_SAIDA.glob("*"):
            f.unlink()
        self.inicio, self.fim, _ = coleta.montar_periodo(PERIODO)
        _, self.total = coleta.buscar_pagina(sessao, self.inicio, self.fim, 0)

    def test_ingenua_perde_e_duplica(self):
        acc, total = coleta.coletar_ingenuo(sessao, self.inicio, self.fim)
        self.assertGreater(total, 1000)
        self.assertLess(len(acc.itens), total)
        self.assertGreater(acc.duplicados, 0)

    def test_adaptativa_pega_tudo_sem_duplicar(self):
        acc = coleta.Acumulador()
        for d0, d1 in coleta.dias_do_periodo(self.inicio, self.fim):
            coleta.coletar_janela(sessao, d0, d1, acc)
        self.assertEqual(len(acc.itens), self.total)
        self.assertEqual(acc.duplicados, 0)
        self.assertEqual(len({r["id"] for r in acc.itens}), self.total)

    def test_retoma_do_ultimo_dia_concluido(self):
        original = coleta.coletar_janela
        chamadas = {"dia": 0}

        def falha_no_segundo_dia(s, inicio, fim, acc, nivel=0):
            if nivel == 1:
                chamadas["dia"] += 1
                if chamadas["dia"] == 2:
                    raise _Interrompido()
            return original(s, inicio, fim, acc, nivel)

        coleta.coletar_janela = falha_no_segundo_dia
        try:
            with self.assertRaises(_Interrompido):
                coleta.cmd_coletar(PERIODO, sessao)
        finally:
            coleta.coletar_janela = original

        parcial = json.loads((coleta.DIR_SAIDA / "bruto_20261001_20261002.json").read_text(encoding="utf-8"))
        self.assertTrue(0 < len(parcial) < self.total)

        _, itens = coleta.cmd_coletar(PERIODO, sessao)
        self.assertEqual(len(itens), self.total)
        self.assertEqual(len({r["id"] for r in itens}), self.total)
        self.assertFalse((coleta.DIR_SAIDA / "bruto_20261001_20261002.dias.json").exists())

    def test_enxugar_tira_campo_pesado(self):
        r = {"id": "1", "contact": {"name": "Ana", "schema": {"x": 1}}}
        self.assertEqual(coleta.enxugar(r), {"id": "1", "contact": {"name": "Ana"}})

    def test_filtro_por_prefixo_absorve_renomeacao(self):
        self.assertTrue(coleta.eh_template_alvo("status_pedido_v1"))
        self.assertTrue(coleta.eh_template_alvo("status_pedido_aprovado"))
        self.assertFalse(coleta.eh_template_alvo("pesquisa_satisfacao"))
        self.assertFalse(coleta.eh_template_alvo(None))

    def test_gravacao_atomica(self):
        destino = coleta.DIR_SAIDA / "lista.json"
        coleta.salvar_json_lista(destino, [{"a": 1}, {"b": "ç"}])
        self.assertEqual(json.loads(destino.read_text(encoding="utf-8")), [{"a": 1}, {"b": "ç"}])
        self.assertFalse(destino.with_name("lista.json.tmp").exists())

    def test_pipeline_completa_gera_relatorio(self):
        coleta.cmd_coletar(PERIODO, sessao)
        _, cache = coleta.cmd_enriquecer(PERIODO, sessao)
        self.assertTrue(cache)
        self.assertTrue(all(isinstance(t, str) and t for t in cache.values()))
        saida = coleta.cmd_gerar(PERIODO)
        self.assertTrue(saida.exists())


if __name__ == "__main__":
    unittest.main()
