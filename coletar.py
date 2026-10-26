#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de notícias regionais (Mococa, São José do Rio Pardo e Casa Branca).

Lê o arquivo fontes.json, tenta primeiro o RSS/Atom de cada fonte e, se não
houver, lê a página de listagem em HTML. Gera o arquivo noticias.json.

Regras:
- Se uma fonte falhar, ela é ignorada e as outras seguem normalmente.
- O resumo vem da descrição publicada pela própria fonte e é cortado em 200 caracteres.
- Remove duplicadas e mantém as 80 mais recentes.
- Notícias já coletadas antes continuam na lista até saírem das 80 mais recentes.

Bibliotecas: feedparser, requests, beautifulsoup4
"""

import html
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- configuração
PASTA = Path(__file__).resolve().parent
ARQ_FONTES = PASTA / "fontes.json"
ARQ_SAIDA = PASTA / "noticias.json"

USER_AGENT = "PainelNoticiasRegionais/1.0 (robo gratuito de noticias regionais; GitHub Actions)"
TIMEOUT = 20             # segundos por requisição
ORCAMENTO_FONTE = 60     # tempo máximo (s) gastando com uma mesma fonte
MAX_NOTICIAS = 80        # quantidade final na lista
MAX_RESUMO = 200         # caracteres do resumo
MAX_POR_FONTE = 40       # notícias lidas por fonte a cada execução
MAX_ENRIQUECER = 15      # páginas de matéria abertas por fonte para achar data/resumo

FUSO = timezone(timedelta(hours=-3))  # horário de Brasília

CIDADES = {
    "Mococa": ["mococa"],
    "São José do Rio Pardo": ["sao jose do rio pardo", "rio pardo"],
    "Casa Branca": ["casa branca"],
}

CAMINHOS_COMUNS = ["feed/", "rss/", "rss.xml", "feed.xml", "atom.xml"]

SESSAO = requests.Session()
SESSAO.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.8",
})


# ----------------------------------------------------------------- utilidades
def normalizar(texto):
    """minúsculas e sem acentos"""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def limpar(texto):
    """tira HTML, entidades e espaços extras"""
    if not texto:
        return ""
    texto = str(texto)
    if "<" in texto and ">" in texto:
        texto = BeautifulSoup(texto, "html.parser").get_text(" ")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def resumir(texto, titulo):
    texto = limpar(texto)
    if not texto or normalizar(texto) == normalizar(titulo):
        return ""
    if len(texto) <= MAX_RESUMO:
        return texto
    corte = texto[: MAX_RESUMO - 1]
    if " " in corte:
        corte = corte.rsplit(" ", 1)[0]
    return corte.rstrip(" ,.;:-") + "…"


def chave_link(link):
    p = urlparse((link or "").strip())
    consulta = [(k, v) for k, v in parse_qsl(p.query)
                if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    texto = p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")
    if consulta:
        texto += "?" + urlencode(sorted(consulta))
    return texto.lower()


def chave_titulo(titulo):
    return re.sub(r"[^a-z0-9]", "", normalizar(titulo))


def detectar_cidade(texto):
    """devolve a cidade citada primeiro no texto, ou '' se nenhuma"""
    t = normalizar(texto)
    melhor, posicao = "", None
    for cidade, termos in CIDADES.items():
        for termo in termos:
            m = re.search(r"\b" + re.escape(termo) + r"\b", t)
            if m and (posicao is None or m.start() < posicao):
                melhor, posicao = cidade, m.start()
    return melhor


def parse_data(valor):
    if not valor:
        return None
    valor = str(valor).strip()
    try:
        d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(valor)
        except (TypeError, ValueError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=FUSO)
    return d.astimezone(FUSO)


def data_de_texto(texto, agora):
    """entende 'Há 3 horas' e '26/10/2026 10h30'"""
    t = normalizar(texto or "")
    m = re.search(r"\bha\s+(\d+)\s*(minuto|min|hora|h|dia|semana|mes)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u.startswith("min"):
            return agora - timedelta(minutes=n)
        if u.startswith("h"):
            return agora - timedelta(hours=n)
        if u.startswith("dia"):
            return agora - timedelta(days=n)
        if u.startswith("sem"):
            return agora - timedelta(weeks=n)
        return agora - timedelta(days=30 * n)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\D{1,6}(\d{1,2})\s*(?:h|:)\s*(\d{2})?)?", t)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=FUSO)
        except ValueError:
            return None
    return None


def baixar(url, prazo):
    restante = prazo - time.monotonic()
    if restante <= 1:
        raise TimeoutError("tempo máximo da fonte esgotado")
    resposta = SESSAO.get(url, timeout=min(TIMEOUT, restante))
    resposta.raise_for_status()
    return resposta


def sem_repetir(lista):
    vistos, saida = set(), []
    for item in lista:
        if item and item not in vistos:
            vistos.add(item)
            saida.append(item)
    return saida


# ----------------------------------------------------------------- leitura por RSS/Atom
def candidatos_feed(url_pagina, soup):
    achados = []
    if soup is not None:
        for tag in soup.find_all("link", href=True):
            rel = tag.get("rel") or []
            rel = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
            tipo = (tag.get("type") or "").lower()
            if "alternate" in rel and ("rss" in tipo or "atom" in tipo):
                achados.append(urljoin(url_pagina, tag["href"]))
    p = urlparse(url_pagina)
    raiz = f"{p.scheme}://{p.netloc}/"
    base = url_pagina if url_pagina.endswith("/") else url_pagina + "/"
    for caminho in CAMINHOS_COMUNS:
        achados.append(urljoin(raiz, caminho))
        if base != raiz:
            achados.append(urljoin(base, caminho))
    return sem_repetir(achados)


def ler_feed(url_feed, prazo):
    resposta = baixar(url_feed, prazo)
    dados = feedparser.parse(resposta.content)
    itens = []
    for e in dados.entries[:MAX_POR_FONTE]:
        titulo = limpar(e.get("title"))
        link = (e.get("link") or "").strip()
        if not titulo or not link:
            continue
        data = None
        for campo in ("published_parsed", "updated_parsed"):
            st = e.get(campo)
            if st:
                data = datetime(*st[:6], tzinfo=timezone.utc).astimezone(FUSO)
                break
        itens.append({
            "titulo": titulo,
            "link": urljoin(url_feed, link),
            "resumo": limpar(e.get("summary") or e.get("description") or ""),
            "data": data,
        })
    return itens


# ----------------------------------------------------------------- leitura por HTML
def ler_html(url, soup, agora):
    dominio = urlparse(url).netloc.removeprefix("www.")
    achados = {}  # chave_link -> item (mantém a ordem da página)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        if a.find_parent(["nav", "footer"]):
            continue
        link = urljoin(url, href).split("#")[0]
        p = urlparse(link)
        if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != dominio:
            continue
        if chave_link(link) == chave_link(url):
            continue
        caminho = p.path.strip("/")
        if not caminho or not ("-" in caminho or re.search(r"\d", caminho)):
            continue

        titulo = limpar(a.get_text(" "))
        if len(titulo) < 25:
            alternativo = limpar(a.get("title", ""))
            if len(alternativo) >= 25:
                titulo = alternativo
        if len(titulo) < 25 or len(titulo.split()) < 4:
            continue

        # contexto do "cartão" da notícia (resumo e data)
        cartao = (a.find_parent("article") or a.find_parent("li")
                  or a.find_parent("div", class_=re.compile(r"post|card|item|noticia|news|materia", re.I)))
        resumo, data = "", None
        if cartao is not None:
            alvo = cartao.select_one('[class*="resumo"], [class*="summary"], [class*="excerpt"], [class*="descricao"]')
            if alvo:
                resumo = limpar(alvo.get_text(" "))
            if not resumo:
                for par in cartao.find_all("p"):
                    txt = limpar(par.get_text(" "))
                    if len(txt) >= 40 and normalizar(txt) not in normalizar(titulo):
                        resumo = txt
                        break
            marca = cartao.find("time")
            if marca is not None:
                data = parse_data(marca.get("datetime")) or data_de_texto(marca.get_text(" "), agora)
            if data is None:
                alvo_data = cartao.select_one('[class*="datetime"], [class*="date"], [class*="data"]')
                if alvo_data:
                    data = data_de_texto(alvo_data.get_text(" "), agora)

        chave = chave_link(link)
        anterior = achados.get(chave)
        if anterior is None or len(titulo) > len(anterior["titulo"]):
            achados[chave] = {"titulo": titulo, "link": link, "resumo": resumo, "data": data}
        if len(achados) >= MAX_POR_FONTE:
            break
    return list(achados.values())


def enriquecer(item, prazo):
    """abre a página da matéria só para ler a descrição e a data publicadas nela"""
    resposta = baixar(item["link"], prazo)
    soup = BeautifulSoup(resposta.content, "html.parser")
    if not item.get("resumo"):
        for seletor in ('meta[property="og:description"]', 'meta[name="description"]',
                        'meta[name="twitter:description"]'):
            tag = soup.select_one(seletor)
            if tag and tag.get("content"):
                item["resumo"] = limpar(tag["content"])
                break
    if item.get("data") is None:
        data = None
        for seletor in ('meta[property="article:published_time"]',
                        'meta[property="og:article:published_time"]',
                        'meta[itemprop="datePublished"]', 'meta[name="date"]',
                        'meta[name="publish-date"]'):
            tag = soup.select_one(seletor)
            if tag and tag.get("content"):
                data = parse_data(tag["content"])
                if data:
                    break
        if not data:
            marca = soup.find("time", attrs={"datetime": True})
            if marca:
                data = parse_data(marca["datetime"])
        if not data:
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', resposta.text)
            if m:
                data = parse_data(m.group(1))
        item["data"] = data


# ----------------------------------------------------------------- uma fonte inteira
def texto_para_busca(item):
    slug = urlparse(item["link"]).path.replace("-", " ").replace("/", " ")
    return f'{item["titulo"]} {item.get("resumo", "")} {slug}'


def coletar_fonte(fonte, cache, agora):
    nome = fonte["nome"]
    url = fonte["endereco"]
    tipo = fonte.get("tipo", "auto")
    prazo = time.monotonic() + ORCAMENTO_FONTE
    soup, itens, modo = None, [], ""

    candidatos = []
    feed_manual = (fonte.get("feed") or "").strip()
    if feed_manual:
        candidatos.append(feed_manual)

    if tipo in ("auto", "rss"):
        try:
            soup = BeautifulSoup(baixar(url, prazo).content, "html.parser")
        except Exception as erro:
            print(f"  aviso: não consegui abrir a página ({erro})")
        candidatos += candidatos_feed(url, soup)
        for candidato in sem_repetir(candidatos):
            try:
                itens = ler_feed(candidato, prazo)
            except Exception:
                continue
            if itens:
                modo = f"RSS {candidato}"
                break

    if not itens and tipo in ("auto", "html"):
        if soup is None:
            soup = BeautifulSoup(baixar(url, prazo).content, "html.parser")
        itens = ler_html(url, soup, agora)
        modo = "HTML"

    if not itens:
        print(f"  nenhuma notícia encontrada nesta fonte")
        return []

    # fontes sem cidade fixa (ex.: R7): só entram notícias que citam as 3 cidades
    if fonte.get("filtrar_por_cidade"):
        filtrados = []
        for item in itens:
            cidade = detectar_cidade(texto_para_busca(item))
            if cidade:
                item["cidade"] = cidade
                filtrados.append(item)
        itens = filtrados

    # completa resumo/data abrindo a matéria (só as que ainda não conhecemos)
    prazo = time.monotonic() + ORCAMENTO_FONTE
    abertas = 0
    for item in itens:
        precisa = (not item.get("resumo")) or item.get("data") is None
        if precisa and chave_link(item["link"]) not in cache and abertas < MAX_ENRIQUECER:
            abertas += 1
            try:
                enriquecer(item, prazo)
            except Exception:
                pass

    finais = [finalizar(i, fonte, cache, agora) for i in itens]
    print(f"  {len(finais)} notícias via {modo}")
    return finais


def finalizar(item, fonte, cache, agora):
    anterior = cache.get(chave_link(item["link"])) or {}
    resumo_bruto = item.get("resumo") or anterior.get("resumo", "")
    data = item.get("data") or parse_data(anterior.get("data")) or agora
    if data > agora:
        data = agora
    cidade = (fonte.get("cidade") or item.get("cidade")
              or detectar_cidade(texto_para_busca({**item, "resumo": resumo_bruto})))
    return {
        "titulo": item["titulo"],
        "resumo": resumir(resumo_bruto, item["titulo"]),
        "fonte": fonte["nome"],
        "cidade": cidade,
        "data": data.isoformat(timespec="seconds"),
        "link": item["link"],
    }


# ----------------------------------------------------------------- programa principal
def carregar_anteriores():
    try:
        dados = json.loads(ARQ_SAIDA.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    lista = [n for n in dados.get("noticias", [])
             if isinstance(n, dict) and not n.get("exemplo") and n.get("link") and n.get("titulo")]
    return lista, dados.get("atualizado_em")


def main():
    agora = datetime.now(FUSO)
    try:
        fontes = json.loads(ARQ_FONTES.read_text(encoding="utf-8"))["fontes"]
    except Exception as erro:
        print(f"ERRO: não consegui ler fontes.json ({erro})")
        return 0

    anteriores, atualizado_antes = carregar_anteriores()
    cache = {chave_link(n["link"]): n for n in anteriores}

    novos = []
    for fonte in fontes:
        if fonte.get("tipo", "auto") not in ("auto", "rss", "html"):
            continue  # ex.: Instagram (só botão na página)
        print(f"[{fonte['nome']}]")
        try:
            novos.extend(coletar_fonte(fonte, cache, agora))
        except Exception as erro:
            print(f"  fonte ignorada: {erro}")

    # junta (novas primeiro), tira duplicadas e ordena da mais nova para a mais antiga
    vistos_link, vistos_titulo, unicos = set(), set(), []
    for n in novos + anteriores:
        kl, kt = chave_link(n["link"]), chave_titulo(n["titulo"])
        if kl in vistos_link or kt in vistos_titulo:
            continue
        vistos_link.add(kl)
        vistos_titulo.add(kt)
        unicos.append(n)
    unicos.sort(key=lambda n: parse_data(n["data"]) or agora, reverse=True)
    lista = unicos[:MAX_NOTICIAS]

    if not lista:
        print("Nenhuma notícia disponível; mantendo o noticias.json como está.")
        return 0

    atualizado_em = atualizado_antes if (lista == anteriores and atualizado_antes) \
        else agora.isoformat(timespec="seconds")

    saida = {"atualizado_em": atualizado_em, "total": len(lista), "noticias": lista}
    ARQ_SAIDA.write_text(json.dumps(saida, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Pronto: {len(lista)} notícias salvas em noticias.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
