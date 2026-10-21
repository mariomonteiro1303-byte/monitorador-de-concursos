"""
Monitor de concursos públicos.
Busca páginas configuradas em fontes.yml, extrai informações disponíveis e envia
um relatório por e-mail. Campos não localizados são marcados como "Não informado".
Importante: cada site tem estrutura própria; os seletores específicos podem ser
configurados em fontes.yml. O programa não deve tratar falha de consulta como
ausência de concursos.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "fontes.yml"
TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (compatible; MonitorConcursos/1.0; "
    "+https://github.com/)"
)
CAMPOS = [
    "Órgão / Instituição",
    "Área de Atuação",
    "Nível de Escolaridade",
    "Número de Vagas / Cadastro Reserva",
    "Remuneração Inicial",
    "Período de Inscrição",
    "Taxa de Inscrição",
    "Banca Organizadora",
    "Local / Abrangência",
    "Data da Prova",
    "Link Oficial do Edital",
]
NAO_INFORMADO = "Não informado"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("monitor_concursos")


@dataclass
class ResultadoFonte:
    nome: str
    url: str
    concursos: list[dict[str, str]]
    erros: list[str]


def carregar_configuracao() -> dict[str, Any]:
    """Carrega o YAML e valida as opções básicas de configuração."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as arquivo:
        config = yaml.safe_load(arquivo) or {}
    if not isinstance(config, dict):
        raise ValueError("O arquivo fontes.yml deve conter um objeto YAML.")
    config.setdefault("fontes", [])
    config.setdefault("estados_prioritarios", ["PI", "CE", "MA", "PE", "BA", "TO"])
    config.setdefault("palavras_chave", ["concurso", "processo seletivo", "edital"])
    if not isinstance(config["fontes"], list):
        raise ValueError("'fontes' deve ser uma lista.")
    return config


def texto_seletor(soup: BeautifulSoup, seletor: str | None) -> str:
    """Obtém texto usando um seletor CSS opcional."""
    if not seletor:
        return ""
    elemento = soup.select_one(seletor)
    return elemento.get_text(" ", strip=True) if elemento else ""


def primeiro_texto(soup: BeautifulSoup, seletores: list[str]) -> str:
    """Tenta uma lista de seletores CSS, retornando o primeiro texto útil."""
    for seletor in seletores:
        valor = texto_seletor(soup, seletor)
        if valor:
            return valor
    return ""


def normalizar_url(url: str, base_url: str) -> str:
    """Resolve URLs relativas e remove fragmentos."""
    absoluta = urljoin(base_url, url.strip())
    partes = urlparse(absoluta)
    return partes._replace(fragment="").geturl()


def link_parece_edital(texto: str, href: str) -> bool:
    """Identifica links que aparentam apontar para edital/documento oficial."""
    alvo = f"{texto} {href}".lower()
    termos = ("edital", ".pdf", "diario oficial", "diário oficial", "retificacao", "retificação")
    return any(termo in alvo for termo in termos)


def extrair_link_edital(soup: BeautifulSoup, pagina_url: str) -> str:
    """Procura links explícitos de edital; não inventa URLs quando não encontra."""
    for ancora in soup.find_all("a", href=True):
        texto = ancora.get_text(" ", strip=True)
        href = ancora.get("href", "")
        if link_parece_edital(texto, href):
            return normalizar_url(href, pagina_url)
    return ""


def extrair_campos(soup: BeautifulSoup, url: str, fonte: dict[str, Any]) -> dict[str, str]:
    """Extrai campos com seletores configuráveis e alternativas heurísticas."""
    seletores = fonte.get("seletores", {}) or {}
    heuristicas = {
        "Órgão / Instituição": ["h1", "main h2", "article h2", "title"],
        "Área de Atuação": [],
        "Nível de Escolaridade": [],
        "Número de Vagas / Cadastro Reserva": [],
        "Remuneração Inicial": [],
        "Período de Inscrição": [],
        "Taxa de Inscrição": [],
        "Banca Organizadora": [],
        "Local / Abrangência": [],
        "Data da Prova": [],
    }
    resultado: dict[str, str] = {}
    for campo in CAMPOS[:-1]:
        configurado = seletores.get(campo)
        if isinstance(configurado, str):
            valor = texto_seletor(soup, configurado)
        elif isinstance(configurado, list):
            valor = primeiro_texto(soup, configurado)
        else:
            valor = primeiro_texto(soup, heuristicas.get(campo, []))
        resultado[campo] = valor or NAO_INFORMADO

    link_configurado = seletores.get("Link Oficial do Edital")
    if isinstance(link_configurado, str):
        link = texto_seletor(soup, link_configurado)
        if link_configurado.startswith("a"):
            elemento = soup.select_one(link_configurado)
            if elemento and elemento.get("href"):
                link = normalizar_url(elemento["href"], url)
    else:
        link = extrair_link_edital(soup, url)
    resultado["Link Oficial do Edital"] = link or NAO_INFORMADO
    return resultado


def atende_filtro(concurso: dict[str, str], config: dict[str, Any]) -> bool:
    """Aplica palavras-chave e estados prioritários ao conteúdo extraído."""
    texto = " ".join(concurso.values()).lower()
    palavras = [str(p).lower() for p in config.get("palavras_chave", [])]
    estados = [str(e).lower() for e in config.get("estados_prioritarios", [])]
    tem_palavra = not palavras or any(p in texto for p in palavras)
    # Sem localização extraída, mantém o item para não descartar oportunidade válida.
    local = concurso.get("Local / Abrangência", "").lower()
    tem_estado = not local or local == NAO_INFORMADO.lower() or any(e in local for e in estados)
    return tem_palavra and tem_estado


def coletar_links(soup: BeautifulSoup, pagina_url: str, fonte: dict[str, Any]) -> list[tuple[str, str]]:
    """Coleta links de detalhes, respeitando seletor opcional da fonte."""
    seletor = fonte.get("seletor_links", "a[href]")
    encontrados: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for ancora in soup.select(seletor):
        href = ancora.get("href")
        titulo = ancora.get_text(" ", strip=True)
        if not href or not titulo:
            continue
        link = normalizar_url(href, pagina_url)
        if urlparse(link).scheme not in ("http", "https") or link in vistos:
            continue
        # Links genéricos de navegação não são úteis para o monitor.
        if len(titulo) < 5 or any(x in link.lower() for x in ("login", "facebook.com", "instagram.com")):
            continue
        vistos.add(link)
        encontrados.append((titulo, link))
    return encontrados


def buscar_fonte(fonte: dict[str, Any], config: dict[str, Any], sessao: requests.Session) -> ResultadoFonte:
    """Consulta uma fonte; registra falhas separadamente de resultados vazios."""
    nome = str(fonte.get("nome", "Fonte sem nome"))
    url = str(fonte.get("url", "")).strip()
    resultados: list[dict[str, str]] = []
    erros: list[str] = []
    if not url.startswith(("https://", "http://")):
        return ResultadoFonte(nome, url, [], ["URL ausente ou inválida na configuração"])

    try:
        resposta = sessao.get(url, timeout=TIMEOUT)
        resposta.raise_for_status()
        resposta.encoding = resposta.apparent_encoding or resposta.encoding
        soup = BeautifulSoup(resposta.text, "html.parser")
    except requests.RequestException as erro:
        mensagem = f"Falha ao consultar {url}: {erro}"
        logger.error(mensagem)
        return ResultadoFonte(nome, url, [], [mensagem])

    # Se a própria página de origem já for uma página de detalhe, usa-a diretamente.
    seletor_detalhe = fonte.get("seletor_detalhe")
    if seletor_detalhe and soup.select_one(seletor_detalhe):
        item = extrair_campos(soup, url, fonte)
        item["Órgão / Instituição"] = (
            item["Órgão / Instituição"]
            if item["Órgão / Instituição"] != NAO_INFORMADO
            else nome
        )
        if atende_filtro(item, config):
            resultados.append(item)
        return ResultadoFonte(nome, url, resultados, erros)

    links = coletar_links(soup, url, fonte)
    max_itens = int(fonte.get("max_links_por_execucao", 30))
    for titulo, link in links[:max_itens]:
        try:
            detalhe_resp = sessao.get(link, timeout=TIMEOUT)
            detalhe_resp.raise_for_status()
            detalhe_resp.encoding = detalhe_resp.apparent_encoding or detalhe_resp.encoding
            detalhe_soup = BeautifulSoup(detalhe_resp.text, "html.parser")
            item = extrair_campos(detalhe_soup, link, fonte)
            if item["Órgão / Instituição"] == NAO_INFORMADO:
                item["Órgão / Instituição"] = titulo
            # Mantém o link da página como referência, mas só o rotula como edital
            # se a página realmente apresentar um link explícito de edital.
            if atende_filtro(item, config):
                resultados.append(item)
        except requests.RequestException as erro:
            erros.append(f"Falha ao consultar detalhe {link}: {erro}")
            logger.warning(erros[-1])
    return ResultadoFonte(nome, url, resultados, erros)


def monitorar_concursos(config: dict[str, Any] | None = None) -> tuple[list[dict[str, str]], list[str]]:
    """Executa todas as fontes configuradas e retorna itens e falhas."""
    config = config or carregar_configuracao()
    fontes = config.get("fontes", [])
    if not fontes:
        raise ValueError(
            "Nenhuma fonte configurada em fontes.yml. Cadastre URLs reais antes de executar."
        )
    sessao = requests.Session()
    sessao.headers.update({"User-Agent": USER_AGENT})
    todos: list[dict[str, str]] = []
    erros: list[str] = []
    for fonte in fontes:
        if not isinstance(fonte, dict):
            erros.append("Uma entrada em 'fontes' não é um objeto válido.")
            continue
        resultado = buscar_fonte(fonte, config, sessao)
        todos.extend(resultado.concursos)
        erros.extend(resultado.erros)

    # Remove duplicatas pelo link oficial, ou pelo nome/local se não houver link.
    unicos: dict[tuple[str, str], dict[str, str]] = {}
    for item in todos:
        chave = (
            item.get("Link Oficial do Edital", NAO_INFORMADO),
            item.get("Órgão / Instituição", NAO_INFORMADO),
        )
        unicos[chave] = item
    return list(unicos.values()), erros


def formatar_relatorio(concursos: list[dict[str, str]], erros: list[str]) -> str:
    """Monta o relatório no formato de 11 campos solicitado pelo usuário."""
    linhas = ["MONITOR DE CONCURSOS PÚBLICOS", "=" * 34, ""]
    if concursos:
        for indice, concurso in enumerate(concursos, start=1):
            linhas.extend([f"OPORTUNIDADE {indice}", "-" * 34])
            for campo in CAMPOS:
                linhas.append(f"{campo}: {concurso.get(campo) or NAO_INFORMADO}")
            linhas.append("")
    elif erros:
        linhas.append("A verificação teve falhas. Não é possível concluir que não existem editais.")
    else:
        linhas.append("Nenhuma oportunidade foi extraída das fontes configuradas nesta execução.")
    if erros:
        linhas.extend(["", "AVISOS DE CONSULTA", "-" * 20])
        linhas.extend(f"- {erro}" for erro in erros)
    return "\n".join(linhas)


def enviar_email(corpo: str) -> None:
    """Envia e-mail se as três variáveis de ambiente estiverem configuradas."""
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA")
    destinatario = os.getenv("EMAIL_DESTINATARIO")
    faltantes = [
        nome for nome, valor in (
            ("EMAIL_REMETENTE", remetente),
            ("EMAIL_SENHA", senha),
            ("EMAIL_DESTINATARIO", destinatario),
        ) if not valor
    ]
    if faltantes:
        raise RuntimeError("Variáveis de e-mail ausentes: " + ", ".join(faltantes))

    mensagem = EmailMessage()
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    mensagem["Subject"] = "Relatório de monitoramento de concursos"
    mensagem.set_content(corpo)

    with smtplib.SMTP(os.getenv("SMTP_SERVIDOR", "smtp.gmail.com"),
                      int(os.getenv("SMTP_PORTA", "587")), timeout=30) as servidor:
        servidor.starttls()
        servidor.login(remetente, senha)
        servidor.send_message(mensagem)
    logger.info("Relatório enviado para %s", destinatario)


def main() -> int:
    """Ponto de entrada: falha explicitamente quando a coleta não pode ser feita."""
    try:
        config = carregar_configuracao()
        concursos, erros = monitorar_concursos(config)
        relatorio = formatar_relatorio(concursos, erros)
        print(relatorio)
        # Não envia e-mail automaticamente em testes locais, salvo se habilitado.
        if os.getenv("ENVIAR_EMAIL", "true").lower() in ("1", "true", "sim", "yes"):
            enviar_email(relatorio)
        # Código de saída diferente de zero alerta o GitHub Actions sobre falhas de fonte.
        return 1 if erros else 0
    except Exception:
        logger.exception("Execução do monitor falhou.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
