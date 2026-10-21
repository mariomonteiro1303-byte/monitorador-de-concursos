"""Monitor de editais em fontes oficiais configuradas.

O programa identifica publicações com aparência de edital de abertura, consulta a
página de detalhes e, quando disponível, extrai texto de PDF. A extração é
heurística: o relatório preserva "Não informado" quando o texto não sustenta um
campo. O link da fonte oficial deve ser conferido pelo candidato.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "fontes.yml"
STATE_PATH = BASE_DIR / "monitor_state.json"
TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; MonitorConcursos/2.0; +https://github.com/)"
NAO_INFORMADO = "Não informado"
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
CAMPO_SITUACAO = "Situação das Inscrições"

# Termos que normalmente indicam atos posteriores, e não abertura de inscrições.
TERMOS_EXCLUIDOS = re.compile(
    r"\b(retifica\w*|resultad\w*|gabarit\w*|convoca\w*|nomea\w*|"
    r"homologa\w*|classifica\w*|impugna\w*|reembols\w*|suspens\w*|"
    r"pedido de isen[cç]\w*|lista de aprovados|respostas? aos recursos|"
    r"recurso contra o resultado)\b",
    re.I,
)
TERMOS_RELEVANTES = re.compile(
    r"\b(edital|concurso p[uú]blico|processo seletivo|sele[cç][aã]o simplificada|"
    r"reda|abertura de inscri[cç][oõ]es|reabertura de inscri[cç][oõ]es)\b",
    re.I,
)
TERMOS_ABERTURA = re.compile(
    r"\b(edital de abertura|abertura do concurso|abertura de inscri[cç][oõ]es|"
    r"concurso p[uú]blico|processo seletivo|sele[cç][aã]o simplificada|reda)\b",
    re.I,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("monitor_concursos")


@dataclass
class ResultadoFonte:
    """Resultado isolado de uma fonte, para distinguir falha de lista vazia."""
    nome: str
    url: str
    concursos: list[dict[str, str]]
    erros: list[str]
    consultada: bool


def carregar_configuracao() -> dict[str, Any]:
    """Lê o YAML e rejeita configurações sem fontes reais habilitadas."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as arquivo:
        config = yaml.safe_load(arquivo) or {}
    if not isinstance(config, dict):
        raise ValueError("fontes.yml deve conter um objeto YAML.")
    fontes = config.get("fontes", [])
    if not isinstance(fontes, list) or not fontes:
        raise ValueError("Nenhuma fonte configurada em fontes.yml.")
    config.setdefault("max_links_por_fonte", 12)
    config.setdefault("somente_abertos_ou_nao_confirmados", True)
    config.setdefault("estados_prioritarios", ["PI", "CE", "MA", "PE", "BA", "TO"])
    return config


def limpar_texto(texto: str) -> str:
    """Normaliza espaços sem modificar o conteúdo semântico."""
    return re.sub(r"\s+", " ", texto or "").strip()


def normalizar_url(url: str, base_url: str) -> str:
    """Transforma caminho relativo em URL absoluta e elimina fragmentos."""
    partes = urlparse(urljoin(base_url, (url or "").strip()))
    if partes.scheme not in ("http", "https"):
        return ""
    return partes._replace(fragment="").geturl()


def tem_termo_excluido(texto: str) -> bool:
    """Evita tratar retificações, resultados e convocações como editais novos."""
    return bool(TERMOS_EXCLUIDOS.search(texto or ""))


def texto_html(html: str) -> tuple[BeautifulSoup, str]:
    """Converte HTML em texto, removendo scripts e elementos de navegação."""
    soup = BeautifulSoup(html, "html.parser")
    for elemento in soup(["script", "style", "noscript", "svg"]):
        elemento.decompose()
    texto = limpar_texto(soup.get_text(" ", strip=True))
    return soup, texto


def extrair_texto_pdf(conteudo: bytes) -> str:
    """Extrai texto de PDF pesquisável. PDFs escaneados exigem leitura manual/OCR."""
    try:
        leitor = PdfReader(BytesIO(conteudo))
        partes = []
        for pagina in leitor.pages[:60]:
            partes.append(pagina.extract_text() or "")
        return limpar_texto(" ".join(partes))
    except Exception as erro:
        # Normaliza erros específicos do leitor de PDF para que uma fonte ruim
        # não encerre a execução completa do monitor.
        raise ValueError(f"Não foi possível extrair texto do PDF: {erro}") from erro


def extrair_valor_apos_rotulo(texto: str, rotulos: list[str], limite: int = 240) -> str:
    """Captura um trecho curto após um rótulo, sem criar conteúdo ausente."""
    for rotulo in rotulos:
        achado = re.search(rotulo, texto, re.I)
        if achado:
            trecho = texto[achado.end():achado.end() + limite]
            trecho = re.split(r"[.;\n](?=\s*[A-ZÁÉÍÓÚÂÊÔÃÕÇ])", trecho, maxsplit=1)[0]
            return limpar_texto(trecho).strip(" :-–")
    return ""


def datas_em(texto: str) -> list[date]:
    """Localiza datas brasileiras e valida cada data identificada."""
    encontradas: list[date] = []
    for dia, mes, ano in re.findall(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", texto):
        try:
            item = date(int(ano), int(mes), int(dia))
            if item not in encontradas:
                encontradas.append(item)
        except ValueError:
            continue
    return encontradas


def extrair_periodo_inscricao(texto: str) -> tuple[str, date | None, date | None]:
    """Procura datas apenas perto de menções a inscrição, evitando datas da prova."""
    for achado in re.finditer(r"inscri[cç][aã]o|inscri[cç][oõ]es|per[ií]odo de inscri", texto, re.I):
        trecho = texto[max(0, achado.start() - 100):achado.end() + 380]
        ds = datas_em(trecho)
        if len(ds) >= 2:
            inicio, fim = ds[0], ds[1]
            return f"{inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}", inicio, fim
        if len(ds) == 1:
            return f"Data relacionada à inscrição identificada: {ds[0].strftime('%d/%m/%Y')}", ds[0], None
    return "", None, None


def classificar_situacao(texto: str, inicio: date | None, fim: date | None) -> str:
    """Classifica com base em datas e termos expressos na fonte, com cautela."""
    agora = date.today()
    janela = texto[:25000]
    # As datas do período de inscrição prevalecem sobre rótulos vagos como "vigente".
    if fim and fim < agora:
        return "Inscrições encerradas (data final anterior a hoje)"
    if inicio and inicio > agora:
        return "Inscrições futuras (confirmar edital)"
    if inicio and fim and inicio <= agora <= fim:
        return "Inscrições abertas (conforme período extraído)"
    if re.search(r"inscri[cç][oõ]es?\s+(est[aã]o\s+)?encerrad|per[ií]odo de inscri[cç][aã]o encerrad", janela, re.I):
        return "Inscrições encerradas (conforme fonte)"
    if re.search(r"inscri[cç][oõ]es?\s+(est[aã]o\s+)?abertas|inscri[cç][oõ]es abertas", janela, re.I):
        return "Inscrições abertas (datas a confirmar no edital)"
    return "Situação não confirmada. Conferir edital oficial"


def extrair_campos(texto: str, titulo: str, edital_url: str, fonte: dict[str, Any]) -> dict[str, str]:
    """Preenche os 11 campos solicitados usando apenas texto encontrado."""
    texto = limpar_texto(texto)
    periodo, inicio, fim = extrair_periodo_inscricao(texto)

    orgao = titulo
    match_orgao = re.search(
        r"((?:PREFEITURA|CÂMARA MUNICIPAL|CAMARA MUNICIPAL|SECRETARIA|UNIVERSIDADE|INSTITUTO|GOVERNO DO ESTADO|TRIBUNAL)[^.;]{0,130})",
        texto, re.I,
    )
    if match_orgao:
        # Se o texto identifica explicitamente o órgão, ele prevalece sobre um título
        # genérico como "Edital de abertura" ou "Concurso Público 001/2026".
        orgao = limpar_texto(match_orgao.group(1))

    nivel = []
    for termo in ("fundamental", "médio", "medio", "técnico", "tecnico", "superior"):
        if re.search(rf"\b{re.escape(termo)}\b", texto, re.I):
            rotulo = "Médio" if termo == "medio" else "Técnico" if termo == "tecnico" else termo.capitalize()
            if rotulo not in nivel:
                nivel.append(rotulo)
    nivel_texto = ", ".join(nivel)

    vagas = ""
    padroes_vagas = [
        r"(.{0,80}\b\d[\d.]*\s+vagas?\b.{0,80})",
        r"(.{0,100}\bcadastro\s+de\s+reserva\b.{0,80})",
        r"(.{0,80}\bforma[cç][aã]o\s+de\s+cadastro\s+reserva\b.{0,80})",
    ]
    for padrao in padroes_vagas:
        m = re.search(padrao, texto, re.I)
        if m:
            vagas = limpar_texto(m.group(1))
            break

    remuneracao = ""
    m = re.search(r"(?:sal[aá]rio|remunera[cç][aã]o|vencimento|subs[ií]dio)[^.;]{0,100}?R\$\s?[\d.]+(?:,[\d]{2})?", texto, re.I)
    if m:
        remuneracao = limpar_texto(m.group(0))
    else:
        m = re.search(r"R\$\s?[\d.]+(?:,[\d]{2})?", texto)
        if m and re.search(r"sal[aá]rio|remunera[cç][aã]o|vencimento|subs[ií]dio", texto[max(0, m.start()-100):m.start()+40], re.I):
            remuneracao = m.group(0)

    taxa = ""
    m = re.search(r"taxa de inscri[cç][aã]o[^.;]{0,100}?R\$\s?[\d.]+(?:,[\d]{2})?", texto, re.I)
    if m:
        taxa = limpar_texto(m.group(0))

    banca = ""
    bancas = ["Cebraspe", "FGV", "Fundação Getulio Vargas", "Fundação Getúlio Vargas",
              "FCC", "Fundação Carlos Chagas", "Instituto AOCP", "AOCP",
              "IBFC", "Consulplan", "Vunesp", "Idecan", "Iades", "Instituto Quadrix",
              "Instituto Legatus", "ICAP", "COPESE", "Uespi"]
    for nome in bancas:
        if re.search(rf"\b{re.escape(nome)}\b", texto, re.I):
            banca = nome
            break

    area = extrair_valor_apos_rotulo(texto, [r"[áa]rea de atua[cç][aã]o\s*[:\-]", r"[áa]rea\s*[:\-]"])
    if not area:
        area = ""

    prova = ""
    m = re.search(r"(?:prova objetiva|prova escrita|data da prova|realiza[cç][aã]o das provas)[^.;]{0,220}", texto, re.I)
    if m:
        ds = datas_em(m.group(0))
        if ds:
            prova = ds[0].strftime("%d/%m/%Y")
        else:
            prova = limpar_texto(m.group(0))

    local = str(fonte.get("local_padrao", ""))
    match_local = re.search(r"(?:local de realiza[cç][aã]o|munic[ií]pio|cidade)\s*[:\-]\s*([^.;]{3,90})", texto, re.I)
    if match_local:
        local = limpar_texto(match_local.group(1))

    situacao = classificar_situacao(texto, inicio, fim)
    resultado = {
        "Órgão / Instituição": orgao or NAO_INFORMADO,
        "Área de Atuação": area or NAO_INFORMADO,
        "Nível de Escolaridade": nivel_texto or NAO_INFORMADO,
        "Número de Vagas / Cadastro Reserva": vagas or NAO_INFORMADO,
        "Remuneração Inicial": remuneracao or NAO_INFORMADO,
        "Período de Inscrição": periodo or NAO_INFORMADO,
        "Taxa de Inscrição": taxa or NAO_INFORMADO,
        "Banca Organizadora": banca or NAO_INFORMADO,
        "Local / Abrangência": local or NAO_INFORMADO,
        "Data da Prova": prova or NAO_INFORMADO,
        "Link Oficial do Edital": edital_url or NAO_INFORMADO,
        CAMPO_SITUACAO: situacao,
        "_titulo_chave": titulo,
    }
    return resultado


def texto_contexto_link(ancora: Any) -> str:
    """Obtém rótulo e contexto curto do link para lidar com botões como 'Baixar'."""
    partes = [ancora.get_text(" ", strip=True)]
    pai = ancora.parent
    nivel = 0
    while pai is not None and nivel < 3:
        # Não subir para blocos globais (body/header) que misturam vários editais.
        # O contexto útil costuma estar na linha ou cartão que contém apenas esse link.
        links_no_bloco = len(pai.find_all("a", href=True)) if hasattr(pai, "find_all") else 0
        texto = limpar_texto(pai.get_text(" ", strip=True))
        if texto and texto not in partes and len(texto) <= 450 and links_no_bloco <= 2:
            partes.append(texto)
        pai = pai.parent
        nivel += 1
    return " | ".join(partes)


def link_candidato(texto_link: str, contexto: str, fonte: dict[str, Any]) -> bool:
    """Aplica padrões por fonte e bloqueia links de atos posteriores."""
    combinado = limpar_texto(f"{texto_link} {contexto}")
    if not combinado or tem_termo_excluido(texto_link):
        return False
    # Em botões genéricos (ex.: "Baixar"), o contexto do cartão identifica o tipo de ato.
    if texto_link.casefold() in {"baixar", "download", "ver", "visualizar", "arquivo", "abrir"} and tem_termo_excluido(contexto):
        return False
    # Permite excluir assuntos não relacionados ao provimento de cargos, como vestibular e monitoria.
    exclusoes_fonte = fonte.get("link_exclude_patterns", [])
    if any(re.search(p, combinado, re.I) for p in exclusoes_fonte):
        return False
    filtros = fonte.get("link_allow_patterns", [])
    if filtros:
        return any(re.search(p, combinado, re.I) for p in filtros)
    return bool(TERMOS_RELEVANTES.search(texto_link))


def coletar_links_candidatos(soup: BeautifulSoup, pagina_url: str, fonte: dict[str, Any], limite: int) -> list[tuple[str, str, str]]:
    """Coleta somente links cuja legenda/contexto sugere abertura de concurso."""
    seletor = fonte.get("seletor_links", "a[href]")
    vistos: set[str] = set()
    resultado = []
    for ancora in soup.select(seletor):
        href = normalizar_url(ancora.get("href", ""), pagina_url)
        if not href or href in vistos:
            continue
        titulo = limpar_texto(ancora.get_text(" ", strip=True))
        contexto = texto_contexto_link(ancora)
        if not link_candidato(titulo, contexto, fonte):
            continue
        # Botões como "Baixar" recebem o nome do concurso do contexto local, se encontrado.
        if titulo.casefold() in {"baixar", "download", "ver", "visualizar", "arquivo", "abrir"}:
            nome_edital = re.search(
                r"((?:EDITAL|CONCURSO P[ÚU]BLICO|PROCESSO SELETIVO|SELE[CÇ][AÃ]O REDA)[^|]{0,180})",
                contexto, re.I,
            )
            if nome_edital:
                titulo = limpar_texto(nome_edital.group(1))
        vistos.add(href)
        resultado.append((titulo or contexto[:100] or "Edital", href, contexto))
        if len(resultado) >= limite:
            break
    return resultado


def baixar_conteudo(url: str, sessao: requests.Session) -> tuple[str, str, str]:
    """Baixa página HTML ou PDF e devolve tipo, texto extraído e URL final."""
    resposta = sessao.get(url, timeout=TIMEOUT, allow_redirects=True)
    resposta.raise_for_status()
    content_type = (resposta.headers.get("Content-Type") or "").lower()
    final_url = resposta.url
    is_pdf = "application/pdf" in content_type or resposta.content[:5] == b"%PDF-" or urlparse(final_url).path.lower().endswith(".pdf")
    if is_pdf:
        return "pdf", extrair_texto_pdf(resposta.content), final_url
    _, texto = texto_html(resposta.text)
    return "html", texto, final_url


def melhor_link_pdf(soup: BeautifulSoup, pagina_url: str, fonte: dict[str, Any]) -> str:
    """Encontra PDF de abertura sem preferir retificações ou resultados."""
    candidatos = []
    for ancora in soup.find_all("a", href=True):
        href = normalizar_url(ancora.get("href", ""), pagina_url)
        texto = limpar_texto(ancora.get_text(" ", strip=True))
        alvo = f"{texto} {href}"
        if not href or tem_termo_excluido(texto):
            continue
        eh_pdf = ".pdf" in urlparse(href).path.lower() or bool(re.search(r"download|arquivo|documento", href, re.I))
        if eh_pdf and (TERMOS_RELEVANTES.search(alvo) or not texto):
            # Links com título de abertura ficam antes de PDFs com rótulos genéricos.
            score = 2 if re.search(r"abertura|edital n|edital nº|edital no", alvo, re.I) else 1
            candidatos.append((score, href))
    if not candidatos:
        return ""
    candidatos.sort(reverse=True)
    return candidatos[0][1]


def consultar_candidato(titulo: str, url: str, contexto: str, fonte: dict[str, Any], sessao: requests.Session) -> dict[str, str] | None:
    """Abre a página do candidato, procura PDF de abertura e extrai os campos."""
    try:
        tipo, texto_detalhe, url_final = baixar_conteudo(url, sessao)
        texto_completo = limpar_texto(f"{contexto} {texto_detalhe}")
        link_oficial = url_final
        if tipo == "html":
            # Prioriza um PDF de abertura explicitamente ligado à página oficial.
            try:
                resposta = sessao.get(url_final, timeout=TIMEOUT)
                resposta.raise_for_status()
                soup, _ = texto_html(resposta.text)
                url_pdf = melhor_link_pdf(soup, url_final, fonte)
                if url_pdf:
                    time.sleep(0.15)  # reduz a frequência de acessos ao portal
                    tipo_pdf, texto_pdf, url_pdf_final = baixar_conteudo(url_pdf, sessao)
                    if tipo_pdf == "pdf" and texto_pdf:
                        texto_completo = limpar_texto(f"{texto_completo} {texto_pdf}")
                        link_oficial = url_pdf_final
            except (requests.RequestException, ValueError) as erro:
                logger.warning("Não foi possível ler PDF ligado a %s: %s", url_final, erro)
        if not texto_completo:
            return None
        concurso = extrair_campos(texto_completo, titulo, link_oficial, fonte)
        # Não publica resultado, retificação, convocação ou ato semelhante como abertura.
        if tem_termo_excluido(titulo):
            return None
        if concurso[CAMPO_SITUACAO].startswith("Inscrições encerradas"):
            return None
        # Não há confirmação suficiente para remover um caso desconhecido. Ele sai com aviso.
        return concurso
    except (requests.RequestException, ValueError, OSError) as erro:
        logger.warning("Falha ao consultar candidato %s (%s): %s", titulo, url, erro)
        return None


def buscar_fonte(fonte: dict[str, Any], sessao: requests.Session, limite_global: int) -> ResultadoFonte:
    """Consulta uma fonte e registra falhas individualmente."""
    nome = str(fonte.get("nome", "Fonte sem nome"))
    url = str(fonte.get("url", "")).strip()
    erros: list[str] = []
    try:
        tipo, texto, url_final = baixar_conteudo(url, sessao)
    except (requests.RequestException, ValueError, OSError) as erro:
        mensagem = f"{nome}: falha de acesso a {url}: {erro}"
        logger.error(mensagem)
        return ResultadoFonte(nome, url, [], [mensagem], False)

    if tipo == "pdf":
        item = extrair_campos(texto, nome, url_final, fonte)
        if not item[CAMPO_SITUACAO].startswith("Inscrições encerradas"):
            return ResultadoFonte(nome, url, [item], [], True)
        return ResultadoFonte(nome, url, [], [], True)

    # Baixa novamente apenas para preservar o DOM e coletar links sem perder a estrutura.
    try:
        resposta = sessao.get(url_final, timeout=TIMEOUT)
        resposta.raise_for_status()
        soup, texto_index = texto_html(resposta.text)
    except requests.RequestException as erro:
        return ResultadoFonte(nome, url, [], [f"{nome}: falha ao interpretar a página: {erro}"], False)

    candidatos = coletar_links_candidatos(soup, url_final, fonte, int(fonte.get("max_links_por_execucao", limite_global)))
    concursos: list[dict[str, str]] = []
    for titulo, link, contexto in candidatos:
        time.sleep(0.15)
        concurso = consultar_candidato(titulo, link, contexto, fonte, sessao)
        if concurso:
            concursos.append(concurso)

    # Algumas páginas já exibem os dados do concurso sem link de detalhe útil.
    # Não tratar o índice inteiro como um edital para evitar falsos positivos.
    if not candidatos:
        logger.info("%s: página acessível, nenhum link de abertura reconhecido.", nome)
    return ResultadoFonte(nome, url, concursos, erros, True)


def normalizar_chave(concurso: dict[str, str]) -> str:
    """Gera uma chave estável baseada no título, órgão/local e fonte do edital."""
    titulo = concurso.get("_titulo_chave", "")
    base = "|".join([
        titulo.casefold().strip(),
        concurso.get("Órgão / Instituição", "").casefold().strip(),
        concurso.get("Local / Abrangência", "").casefold().strip(),
    ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def assinatura(concurso: dict[str, str]) -> str:
    """Detecta mudanças materiais nos campos do edital para avisar novamente."""
    dados = {k: v for k, v in concurso.items() if not k.startswith("_")}
    serializado = json.dumps(dados, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def carregar_estado() -> dict[str, Any]:
    """Lê o registro persistente de editais já enviados em execuções anteriores."""
    if not STATE_PATH.exists():
        return {"items": {}}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as arquivo:
            estado = json.load(arquivo)
        if isinstance(estado, dict) and isinstance(estado.get("items"), dict):
            return estado
    except (OSError, json.JSONDecodeError):
        logger.warning("Estado persistente inválido. Será iniciado um registro novo.")
    return {"items": {}}


def separar_novos(concursos: list[dict[str, str]], estado: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Retorna concursos novos/alterados e prepara estado atualizado para persistência."""
    itens = dict(estado.get("items", {}))
    novos: list[dict[str, str]] = []
    agora = datetime.now(ZoneInfo("America/Fortaleza")).isoformat(timespec="seconds")
    for concurso in concursos:
        chave = normalizar_chave(concurso)
        sig = assinatura(concurso)
        anterior = itens.get(chave, {})
        if anterior.get("signature") != sig:
            concurso["_novidade"] = "Atualizado" if anterior else "Novo"
            novos.append(concurso)
            itens[chave] = {
                "signature": sig,
                "title": concurso.get("_titulo_chave", ""),
                "last_seen": agora,
                "last_notified": agora,
            }
        # Para itens sem alteração, mantém o registro exatamente igual para evitar
        # um commit diário desnecessário apenas por atualizar timestamps.
    # Limita o estado aos 2.000 registros mais recentes para não crescer sem limite.
    if len(itens) > 2000:
        itens = dict(sorted(itens.items(), key=lambda par: par[1].get("last_seen", ""))[-2000:])
    updated_at = agora if novos else estado.get("updated_at")
    return novos, {"updated_at": updated_at, "items": itens}


def salvar_estado(estado: dict[str, Any]) -> None:
    """Grava arquivo que o workflow de GitHub Actions poderá commitar no repositório."""
    temporario = STATE_PATH.with_suffix(".tmp")
    temporario.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(STATE_PATH)


def formatar_relatorio(concursos: list[dict[str, str]], erros: list[str], total_encontrado: int) -> str:
    """Monta o relatório com os 11 campos obrigatórios e uma situação complementar."""
    linhas = [
        "MONITOR DE ABERTURA DE EDITAIS DE CONCURSOS PÚBLICOS",
        f"Verificação: {datetime.now(ZoneInfo('America/Fortaleza')).strftime('%d/%m/%Y %H:%M %Z')}",
        f"Oportunidades novas/alteradas nesta execução: {len(concursos)}",
        "",
    ]
    if concursos:
        for indice, concurso in enumerate(concursos, start=1):
            novidade = concurso.get("_novidade", "Oportunidade")
            linhas.extend([f"{novidade.upper()} {indice}: {concurso.get('_titulo_chave', '')}", "-" * 48])
            for campo in CAMPOS:
                linhas.append(f"{campo}: {concurso.get(campo) or NAO_INFORMADO}")
            linhas.append(f"{CAMPO_SITUACAO}: {concurso.get(CAMPO_SITUACAO, 'Situação não confirmada')}")
            if concurso.get(CAMPO_SITUACAO, "").startswith("Situação não confirmada"):
                linhas.append("Atenção: confirme o período de inscrições no edital oficial antes de se inscrever.")
            linhas.append("")
    elif erros and total_encontrado == 0:
        linhas.append("A consulta teve falhas. Não é possível concluir que não há novos editais.")
    else:
        linhas.append("Nenhum edital novo/alterado e elegível foi identificado nas fontes consultadas.")
    if erros:
        linhas.extend(["", "AVISOS DE CONSULTA", "-" * 24])
        linhas.extend(f"- {erro}" for erro in erros)
    return "\n".join(linhas)


def enviar_email(corpo: str) -> None:
    """Envia mensagem via Gmail SMTP e propaga falhas para o status do workflow."""
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA")
    destinatario = os.getenv("EMAIL_DESTINATARIO")
    faltantes = [nome for nome, valor in (
        ("EMAIL_REMETENTE", remetente), ("EMAIL_SENHA", senha),
        ("EMAIL_DESTINATARIO", destinatario),
    ) if not valor]
    if faltantes:
        raise RuntimeError("Segredos/variáveis de e-mail ausentes: " + ", ".join(faltantes))

    mensagem = EmailMessage()
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    mensagem["Subject"] = "Monitor de Concursos: relatório de editais"
    mensagem.set_content(corpo)
    servidor = os.getenv("SMTP_SERVIDOR", "smtp.gmail.com")
    porta = int(os.getenv("SMTP_PORTA", "587"))
    # O context manager fecha corretamente a conexão, inclusive em falhas de login/envio.
    with smtplib.SMTP(servidor, porta, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(remetente, senha)
        smtp.send_message(mensagem)
    logger.info("Mensagem aceita pelo servidor SMTP para %s", destinatario)


def main() -> int:
    """Executa consulta, notifica novos editais e registra estado somente após envio."""
    try:
        config = carregar_configuracao()
        sessao = requests.Session()
        sessao.headers.update({"User-Agent": USER_AGENT})
        resultados: list[dict[str, str]] = []
        erros: list[str] = []
        fontes_ok = 0
        limite = int(config.get("max_links_por_fonte", 12))
        for fonte in config["fontes"]:
            if not isinstance(fonte, dict) or not fonte.get("url"):
                erros.append("Entrada de fonte inválida ou sem URL.")
                continue
            resultado = buscar_fonte(fonte, sessao, limite)
            resultados.extend(resultado.concursos)
            erros.extend(resultado.erros)
            fontes_ok += int(resultado.consultada)
            logger.info("Fonte %s: %d itens extraídos; consultada=%s", resultado.nome, len(resultado.concursos), resultado.consultada)

        # Remove duplicatas da mesma execução usando o identificador estável.
        unicos: dict[str, dict[str, str]] = {}
        for item in resultados:
            unicos[normalizar_chave(item)] = item
        resultados = list(unicos.values())
        estado_anterior = carregar_estado()
        novos, estado_novo = separar_novos(resultados, estado_anterior)
        relatorio = formatar_relatorio(novos, erros, len(resultados))
        print(relatorio)

        # Envia relatório de sucesso ou relatório de falha; a falha SMTP encerra o job.
        if os.getenv("ENVIAR_EMAIL", "true").lower() in ("1", "true", "sim", "yes"):
            enviar_email(relatorio)
            # Só atualiza o estado depois do SMTP aceitar o envio para evitar perder alertas.
            if fontes_ok > 0:
                salvar_estado(estado_novo)

        # Qualquer fonte que falhou fica visível como falha do workflow após o relatório.
        if fontes_ok == 0:
            logger.error("Nenhuma fonte foi consultada com sucesso.")
            return 1
        if erros:
            logger.error("Uma ou mais fontes falharam. Verifique os avisos do relatório.")
            return 1
        return 0
    except Exception:
        logger.exception("Execução do monitor falhou.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
