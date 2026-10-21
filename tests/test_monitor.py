"""Testes unitários sem depender da internet ou de credenciais SMTP."""
import unittest
from datetime import date, timedelta
from unittest.mock import Mock
from bs4 import BeautifulSoup

import monitor


class TestMonitor(unittest.TestCase):
    def test_tem_11_campos_padronizados(self):
        self.assertEqual(len(monitor.CAMPOS), 11)
        self.assertEqual(monitor.CAMPOS[0], "Órgão / Instituição")
        self.assertEqual(monitor.CAMPOS[-1], "Link Oficial do Edital")

    def test_link_relativo_e_normalizado(self):
        self.assertEqual(
            monitor.normalizar_url("/documentos/edital.pdf", "https://orgao.gov.br/concursos"),
            "https://orgao.gov.br/documentos/edital.pdf",
        )

    def test_exclui_resultados_e_retificacoes(self):
        self.assertTrue(monitor.tem_termo_excluido("Resultado final do concurso"))
        self.assertTrue(monitor.tem_termo_excluido("Resultados preliminares"))
        self.assertTrue(monitor.tem_termo_excluido("Retificações do edital 02/2026"))
        self.assertTrue(monitor.tem_termo_excluido("Convocações de candidatos"))
        self.assertFalse(monitor.tem_termo_excluido("Edital de abertura do concurso"))

    def test_extrai_periodo_de_inscricao_perto_do_rotulo(self):
        texto = "Inscrições de 01/11/2026 a 30/11/2026. A prova objetiva será em 20/12/2026."
        periodo, inicio, fim = monitor.extrair_periodo_inscricao(texto)
        self.assertEqual(periodo, "01/11/2026 a 30/11/2026")
        self.assertEqual(inicio, date(2026, 11, 1))
        self.assertEqual(fim, date(2026, 11, 30))

    def test_classifica_periodo_expirado(self):
        ontem = date.today() - timedelta(days=1)
        anteontem = ontem - timedelta(days=10)
        status = monitor.classificar_situacao("Edital publicado", anteontem, ontem)
        self.assertTrue(status.startswith("Inscrições encerradas"))

    def test_extrai_campos_sem_inventar_dados(self):
        texto = (
            "PREFEITURA MUNICIPAL DE TESTE. Concurso Público para provimento de vagas. "
            "Níveis fundamental e superior. São oferecidas 12 vagas. Taxa de inscrição R$ 90,00. "
            "Inscrições de 01/11/2026 a 30/11/2026. Prova objetiva em 20/12/2026. "
            "Remuneração inicial R$ 2.500,00. Banca Instituto AOCP."
        )
        item = monitor.extrair_campos(texto, "Edital de abertura 2026", "https://prefeitura.gov.br/edital.pdf", {"local_padrao": "Teste, PI"})
        self.assertEqual(item["Órgão / Instituição"], "PREFEITURA MUNICIPAL DE TESTE")
        self.assertIn("fundamental", item["Nível de Escolaridade"].lower())
        self.assertIn("12 vagas", item["Número de Vagas / Cadastro Reserva"])
        self.assertEqual(item["Link Oficial do Edital"], "https://prefeitura.gov.br/edital.pdf")
        self.assertEqual(item["Taxa de Inscrição"].lower(), "taxa de inscrição r$ 90,00")
        self.assertEqual(item["Banca Organizadora"], "Instituto AOCP")

    def test_coleta_links_apenas_relevantes(self):
        html = '''<html><body>
          <a href="/edital-abertura-2026.pdf">Edital de abertura 2026</a>
          <a href="/resultado-final.pdf">Resultado final do concurso</a>
          <a href="/inicio">Início</a>
        </body></html>'''
        soup = BeautifulSoup(html, "html.parser")
        fonte = {"link_allow_patterns": ["edital de abertura"]}
        links = monitor.coletar_links_candidatos(soup, "https://orgao.gov.br/concursos", fonte, 10)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1], "https://orgao.gov.br/edital-abertura-2026.pdf")

    def test_link_generico_baixar_usa_nome_do_edital_no_contexto(self):
        html = '<html><body><section><h2>CONCURSO PÚBLICO 001/2026 - Prefeitura Municipal</h2><a href="/edital.pdf">Baixar</a></section></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        fonte = {"link_allow_patterns": ["CONCURSO PÚBLICO"]}
        links = monitor.coletar_links_candidatos(soup, "https://prefeitura.ma.gov.br/concursos", fonte, 10)
        self.assertEqual(len(links), 1)
        self.assertIn("CONCURSO PÚBLICO", links[0][0].upper())

    def test_exclusao_especifica_da_fonte(self):
        self.assertFalse(monitor.link_candidato(
            "Processo Seletivo de Monitoria Acadêmica",
            "Edital processo seletivo de monitoria",
            {"link_allow_patterns": ["processo seletivo"], "link_exclude_patterns": ["monitoria"]},
        ))

    def test_estado_identifica_novo_e_sem_mudanca(self):
        item = {"_titulo_chave": "Edital de abertura 2026", "Órgão / Instituição": "Órgão X", "Local / Abrangência": "PI", "Link Oficial do Edital": "https://gov.br/edital.pdf"}
        novos, estado = monitor.separar_novos([item.copy()], {"items": {}})
        self.assertEqual(len(novos), 1)
        self.assertEqual(novos[0]["_novidade"], "Novo")
        novos_2, estado_2 = monitor.separar_novos([item.copy()], estado)
        self.assertEqual(len(novos_2), 0)
        self.assertEqual(estado_2, estado)

    def test_relatorio_contem_11_campos_e_alerta_sobre_fonte(self):
        item = {campo: "Não informado" for campo in monitor.CAMPOS}
        item.update({"_titulo_chave": "Edital Teste", "_novidade": "Novo", monitor.CAMPO_SITUACAO: "Situação não confirmada. Conferir edital oficial"})
        texto = monitor.formatar_relatorio([item], ["Fonte X indisponível"], 1)
        for campo in monitor.CAMPOS:
            self.assertIn(campo + ":", texto)
        self.assertIn("Fonte X indisponível", texto)
        self.assertIn("confirme o período de inscrições", texto.lower())

    def test_consulta_fonte_completa_com_respostas_falsas(self):
        # Testa o fluxo de índice -> detalhe HTML sem acessar a internet.
        class RespostaFalsa:
            def __init__(self, url, html):
                self.url = url
                self.text = html
                self.content = html.encode("utf-8")
                self.headers = {"Content-Type": "text/html; charset=utf-8"}
                self.apparent_encoding = "utf-8"

            def raise_for_status(self):
                return None

        indice_url = "https://prefeitura.gov.br/concursos"
        detalhe_url = "https://prefeitura.gov.br/edital-abertura-2026"
        indice = '<html><body><main><a href="/edital-abertura-2026">Edital de abertura 2026</a></main></body></html>'
        detalhe = (
            '<html><body><main><h1>Prefeitura Municipal de Teste</h1>'
            '<p>Concurso público para provimento de vagas. Nível superior. '
            'Remuneração inicial R$ 3.000,00. '
            'Inscrições de 01/11/2026 a 30/11/2026. '
            'Taxa de inscrição R$ 100,00. Prova objetiva em 20/12/2026. '
            'Banca Instituto AOCP.</p></main></body></html>'
        )

        class SessaoFalsa:
            headers = {}
            def get(self, url, **kwargs):
                if url == indice_url:
                    return RespostaFalsa(indice_url, indice)
                if url == detalhe_url:
                    return RespostaFalsa(detalhe_url, detalhe)
                raise AssertionError(f"URL inesperada no teste: {url}")

        fonte = {
            "nome": "Prefeitura teste",
            "url": indice_url,
            "local_padrao": "Município de Teste, PI",
            "link_allow_patterns": ["edital de abertura"],
            "max_links_por_execucao": 4,
        }
        resultado = monitor.buscar_fonte(fonte, SessaoFalsa(), 4)
        self.assertTrue(resultado.consultada)
        self.assertEqual(len(resultado.concursos), 1)
        item = resultado.concursos[0]
        self.assertEqual(item["Período de Inscrição"], "01/11/2026 a 30/11/2026")
        self.assertEqual(item["Data da Prova"], "20/12/2026")
        self.assertEqual(item["Link Oficial do Edital"], detalhe_url)
        self.assertTrue(item[monitor.CAMPO_SITUACAO].startswith("Inscrições futuras"))

    def test_http_request_nao_sucesso_nao_vira_lista_vazia(self):
        sessao = Mock()
        sessao.get.side_effect = monitor.requests.ConnectionError("sem conexão")
        resultado = monitor.buscar_fonte({"nome": "Fonte teste", "url": "https://fonte.gov.br"}, sessao, 10)
        self.assertFalse(resultado.consultada)
        self.assertTrue(resultado.erros)


if __name__ == "__main__":
    unittest.main(verbosity=2)
