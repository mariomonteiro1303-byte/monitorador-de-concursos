"""Testes automatizados básicos sem acessar a internet ou enviar e-mail."""
import unittest
from bs4 import BeautifulSoup

import monitor


class TestMonitor(unittest.TestCase):
    def test_formato_tem_11_campos(self):
        self.assertEqual(len(monitor.CAMPOS), 11)

    def test_extrai_titulo_e_link_edital(self):
        html = """
        <html><head><title>Concurso de Teste</title></head>
        <body><h1>Prefeitura de Exemplo</h1>
        <a href='/docs/edital.pdf'>Edital de abertura PDF</a></body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        resultado = monitor.extrair_campos(soup, "https://exemplo.gov.br/concursos", {})
        self.assertEqual(resultado["Órgão / Instituição"], "Prefeitura de Exemplo")
        self.assertEqual(
            resultado["Link Oficial do Edital"],
            "https://exemplo.gov.br/docs/edital.pdf",
        )
        self.assertEqual(resultado["Taxa de Inscrição"], monitor.NAO_INFORMADO)

    def test_relatorio_inclui_os_campos(self):
        concurso = {campo: "Não informado" for campo in monitor.CAMPOS}
        concurso["Órgão / Instituição"] = "Órgão de Teste"
        relatorio = monitor.formatar_relatorio([concurso], [])
        for campo in monitor.CAMPOS:
            self.assertIn(campo + ":", relatorio)

    def test_falha_na_consulta_nao_e_mensagem_de_lista_vazia(self):
        texto = monitor.formatar_relatorio([], ["Falha ao consultar fonte"])
        self.assertIn("verificação teve falhas", texto)
        self.assertIn("Falha ao consultar fonte", texto)

    def test_filtro_preserva_local_desconhecido(self):
        config = {"estados_prioritarios": ["PI"], "palavras_chave": ["concurso"]}
        concurso = {
            "Órgão / Instituição": "Concurso de Teste",
            "Local / Abrangência": "Não informado",
        }
        self.assertTrue(monitor.atende_filtro(concurso, config))


if __name__ == "__main__":
    unittest.main()
