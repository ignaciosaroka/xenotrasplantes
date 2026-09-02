#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 CLIPPING SEMANAL DE XENOTRASPLANTES
================================================================================

 Qué hace, en orden:

   1. Recorre una lista de búsquedas (en español y en inglés) sobre
      xenotrasplantes usando los feeds RSS de Google News.
   2. Opcionalmente consulta PubMed para literatura científica nueva.
   3. Junta todo, elimina duplicados (la misma noticia replicada en 50 medios).
   4. Descarta lo que ya salió en informes anteriores (base de datos local).
   5. Le pide a Claude que descarte el ruido, clasifique cada nota y la
      resuma en dos líneas.
   6. Arma un PDF con el informe.

 Todo lo que se configura está en el bloque CONFIGURACION, más abajo.
 No hace falta tocar nada más.

================================================================================
"""

import os
import re
import sys
import json
import argparse
import time
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import feedparser
from anthropic import Anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, KeepTogether
)


# ==============================================================================
#  CONFIGURACION
# ==============================================================================

# --- Ventana de tiempo -------------------------------------------------------
# Cuántos días hacia atrás mirar. 7 = una semana.
VENTANA_DIAS = 7

# --- Tramos de búsqueda -------------------------------------------------------
# Google News devuelve un tope de resultados por consulta. Si se le pide un mes
# entero, ese tope se llena con lo más reciente y lo anterior se pierde. Por eso
# las ventanas largas se parten en tramos de esta cantidad de días, y cada tramo
# es una consulta propia con su propio tope.
# Poner en 0 para volver al comportamiento anterior (una sola consulta).
TRAMO_DIAS = 7

# --- Dónde se guardan las cosas ----------------------------------------------
# Por defecto, una subcarpeta "salida" al lado del script. Así el mismo
# archivo funciona en tu PC y en un servidor, sin tocar nada.
# Podés forzar otra ruta con la variable de entorno XENO_SALIDA.
CARPETA_SALIDA = os.environ.get(
    "XENO_SALIDA",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "salida"))

# --- Clave de la API de Anthropic --------------------------------------------
# Se lee SIEMPRE de la variable de entorno ANTHROPIC_API_KEY.
# NUNCA pegues la clave dentro de este archivo: si el archivo va a un
# repositorio, la clave queda expuesta y hay que darla de baja.
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Modelo. Haiku es barato y alcanza de sobra para esta tarea.
# Si querés resúmenes más finos, cambiá a "claude-sonnet-5".
MODELO = "claude-haiku-4-5-20251001"

# --- Búsquedas ---------------------------------------------------------------
# Cada línea es una búsqueda distinta que se manda a Google News.
# Las comillas dobles internas fuerzan la frase exacta.
# Agregá o sacá lo que quieras: es la parte más importante del sistema.

BUSQUEDAS_ES = [
    'xenotrasplante',
    'xenotrasplantes',
    '"trasplante de cerdo"',
    '"órgano de cerdo" trasplante',
    '"riñón de cerdo"',
    '"corazón de cerdo" trasplante',
    '"hígado de cerdo" trasplante',
    '"cerdo modificado genéticamente"',
    '"cerdo editado genéticamente"',
    '"triple knockout" cerdo',
    'xenotrasplante ensayo clínico',
    'xenotrasplante bioética',
    'trasplante animal humano rechazo',
]

BUSQUEDAS_EN = [
    'xenotransplantation',
    '"pig kidney transplant"',
    '"pig heart transplant"',
    '"pig liver transplant"',
    '"pig lung transplant"',
    '"gene-edited pig" organ',
    '"xenotransplant" clinical trial',
    '"xenokidney" OR "UKidney" OR "UHeart"',
    'pig organ transplant FDA',
    'xenotransplantation ethics',
    'PERV porcine endogenous retrovirus',
    'decedent study pig organ',
]

# Búsquedas por nombre propio. Esta es la capa que más rinde: muchos
# comunicados y papers no usan la palabra "xenotrasplante" en el titular,
# pero sí nombran a la empresa, al centro médico o al investigador.
BUSQUEDAS_ACTORES = [
    # empresas
    'eGenesis xenotransplant',
    '"United Therapeutics" xenotransplant',
    'Revivicor pig organ',
    '"Makana Therapeutics"',
    '"Qihan Biotech"',
    '"Clonorgan"',
    '"CrofaBiotech"',
    # centros
    '"NYU Langone" xenotransplant',
    '"Massachusetts General" pig transplant',
    '"University of Maryland" pig heart',
    '"UAB" pig kidney transplant',
    '"Xijing Hospital" xenotransplant',
    # investigadores
    '"Muhammad Mohiuddin" transplant',
    '"Robert Montgomery" xenotransplant',
    '"Jayme Locke" xenotransplant',
    '"David Cooper" xenotransplantation',
    '"Adrián Mutto" OR "Adrian Mutto"',
    # reguladores y organismos
    'FDA xenotransplantation',
    'INCUCAI xenotrasplante',
    'WHO xenotransplantation',
]

# --- Literatura científica: Europe PMC -----------------------------------------
# Europe PMC indexa PubMed + preprints (bioRxiv, medRxiv) en una sola consulta.
# API pública, sin clave, sin registro.
INCLUIR_CIENCIA = True
# OJO con la palabra "xenograft" suelta: en oncología significa tumor humano
# implantado en ratón (modelos PDX) y hay miles de papers por año sin ninguna
# relación con esto. Por eso sólo se acepta si viene acompañada de cerdo.
CONSULTA_CIENCIA = (
    '(xenotransplantation OR xenotransplant OR xenokidney OR xenoheart '
    'OR "pig-to-human" OR "porcine-to-human" OR "pig organ" '
    'OR "porcine organ" OR "gene-edited pig" OR "pig kidney" '
    'OR "pig heart transplant" '
    'OR (xenograft AND (pig OR porcine OR swine)))'
)
MAX_CIENCIA = 60

# --- Ensayos clínicos: ClinicalTrials.gov --------------------------------------
# Registro oficial de EE.UU. Acá los ensayos aparecen antes que en prensa.
# API pública, sin clave.
INCLUIR_ENSAYOS = True
# Mismo cuidado: sin "xenograft" suelto, que trae ensayos oncológicos.
CONSULTA_ENSAYOS = ('xenotransplantation OR xenotransplant OR xenokidney '
                    'OR "pig kidney" OR "pig heart" OR "porcine organ"')

# --- Patentes: PatentsView ------------------------------------------------------
# OPCIONAL. Requiere una clave gratuita que se pide en:
#   https://patentsview.org/apis/keyrequest
# Si dejás la clave vacía, el script simplemente se saltea esta fuente.
# Las patentes muestran líneas porcinas y técnicas años antes que los papers.
CLAVE_PATENTES = os.environ.get("PATENTSVIEW_API_KEY", "")
CONSULTA_PATENTES = "xenotransplantation"

# --- Feeds directos de medios especializados e instituciones ------------------
# Google News indexa mal lo que está detrás de un muro de pago, y en cambio
# indexa bien al agregador que reescribe la misma noticia sin muro. Resultado:
# tiene un sesgo hacia las fuentes de menor valor. Estos feeds entran por su
# cuenta para corregir eso.
#
# Como estos medios cubren todo el sector salud y no sólo xenotrasplantes,
# después se filtran por palabra clave (ver FILTRO_FEEDS más abajo).
INCLUIR_FEEDS_DIRECTOS = True

FEEDS_DIRECTOS = [
    # --- reporteo original (nivel 2) ---
    # OJO: estos feeds sólo traen los últimos 10-25 artículos, o sea unas
    # pocas horas de producción. No alcanzan para cubrir una semana; por eso
    # además se los busca por sitio en BUSQUEDAS_SITIOS, más abajo.
    ("STAT News",            "https://www.statnews.com/feed/", 2),
    ("Fierce Biotech",       "https://www.fiercebiotech.com/rss/xml", 2),
    ("Endpoints News",       "https://endpoints.news/feed/", 2),
    ("NPR Salud",            "https://feeds.npr.org/1128/rss.xml", 2),
    ("MIT Technology Review",
     "https://www.technologyreview.com/topic/biotechnology/feed", 2),
    ("Nature (noticias)",    "https://www.nature.com/nature.rss", 2),
    ("Science (noticias)",   "https://www.science.org/rss/news_current.xml", 2),
]


# Un ítem de estos feeds sólo se conserva si su título o resumen menciona
# alguna de estas palabras. Sin esto entraría todo el sector salud.
FILTRO_FEEDS = [
    "xenotransplant", "xenotransplantation", "xenograft", "xenotrasplante",
    "pig kidney", "pig heart", "pig liver", "pig lung", "pig organ",
    "porcine organ", "porcine kidney", "gene-edited pig", "gene edited pig",
    "genetically modified pig", "egenesis", "revivicor", "united therapeutics",
    "clonorgan", "makana", "qihan", "choironex", "organ shortage",
    "cerdo", "porcino",
]

# --- Niveles de fuente ---------------------------------------------------------
# 1 = fuente primaria: acá nace la noticia (comunicados, registros, revistas).
# 2 = reporteo original: alguien verifica, llama, agrega contexto.
# 3 = reescritura: reformula lo ajeno sin agregar nada. Es de donde salen los
#     errores más caros, como un hecho de 2024 republicado como novedad.
# Lo que no esté en esta lista se marca como nivel desconocido y se trata
# con la misma cautela que el 3.
NIVEL_POR_DOMINIO = {
    # nivel 1 — primarias
    "clinicaltrials.gov": 1, "europepmc.org": 1, "doi.org": 1,
    "pubmed.ncbi.nlm.nih.gov": 1, "biorxiv.org": 1, "medrxiv.org": 1,
    "businesswire.com": 1, "prnewswire.com": 1, "globenewswire.com": 1,
    "nyulangone.org": 1, "massgeneralbrigham.org": 1, "fda.gov": 1,
    "nih.gov": 1, "who.int": 1, "ema.europa.eu": 1, "argentina.gob.ar": 1,
    "unitedtherapeutics.com": 1, "egenesis.com": 1, "harvard.edu": 1,
    "uba.ar": 1, "unsam.edu.ar": 1, "patents.google.com": 1,

    # nivel 2 — reporteo original
    "statnews.com": 2, "endpoints.news": 2, "fiercebiotech.com": 2,
    "fiercepharma.com": 2, "npr.org": 2, "technologyreview.com": 2,
    "nature.com": 2, "science.org": 2, "nytimes.com": 2,
    "washingtonpost.com": 2, "wsj.com": 2, "reuters.com": 2, "apnews.com": 2,
    "bloomberg.com": 2, "ft.com": 2, "theguardian.com": 2, "bbc.com": 2,
    "bbc.co.uk": 2, "japantimes.co.jp": 2, "nippon.com": 2, "elpais.com": 2,
    "lanacion.com.ar": 2, "clarin.com": 2, "infobae.com": 2,
    "scientificamerican.com": 2, "newscientist.com": 2, "wired.com": 2,
    "medscape.com": 2, "kffhealthnews.org": 2, "cnn.com": 2, "nbcnews.com": 2,
}

# Dominios que ya demostraron no aportar: terminales de datos financieros,
# consultoras de informes de mercado y agregadores que reciclan sin fechar.
DOMINIOS_EXCLUIDOS = [
    "simplywall.st", "marketscreener.com", "tradingview.com",
    "futuremarketinsights.com", "marketresearch", "researchandmarkets.com",
    "openpr.com", "einpresswire.com", "dagens.com", "msn.com",
    "investing.com", "zacks.com", "benzinga.com", "stocktwits.com",
]

# --- Búsquedas por sitio ------------------------------------------------------
# Los feeds de arriba sólo traen las últimas horas de publicación, así que se
# los busca TAMBIÉN por sitio dentro de Google News. Esto cubre además a las
# instituciones, cuyos RSS propios cambian de dirección o directamente no
# existen: los comunicados igual quedan indexados.
#
# Cada línea es un dominio. El script arma la consulta solo.
SITIOS_PRIORITARIOS = [
    # reporteo original
    "statnews.com", "endpoints.news", "fiercebiotech.com",
    "technologyreview.com", "npr.org", "nature.com", "science.org",
    "nytimes.com", "washingtonpost.com", "reuters.com", "apnews.com",
    # instituciones y empresas (fuente primaria)
    "nyulangone.org", "massgeneralbrigham.org", "hopkinsmedicine.org",
    "medschool.umaryland.edu", "uab.edu", "nih.gov", "fda.gov",
    "unitedtherapeutics.com", "egenesis.com", "businesswire.com",
    "prnewswire.com",
]

# Las búsquedas por sitio están APAGADAS por defecto. Motivo: Google ignora
# con frecuencia el grupo de términos en una consulta site:, y devuelve las
# noticias generales del medio. En una corrida de 30 días dieron 70
# resultados fuera de tema y casi ninguno útil: gastan presupuesto de
# consultas sin aportar. El presupuesto se usa mejor en los tramos (ver
# TRAMO_DIAS). Poner en True si alguna vez Google cambia de comportamiento.
INCLUIR_BUSQUEDAS_SITIOS = False

# Términos que se combinan con cada sitio. Van en una sola consulta para no
# multiplicar el número de búsquedas.
TERMINOS_SITIOS = ('xenotransplantation OR xenotransplant OR "pig kidney" '
                   'OR "pig heart" OR "pig liver" OR "pig organ" '
                   'OR "gene-edited pig"')

# --- Jerarquía de fuentes -----------------------------------------------------
# Nivel 1 = fuente primaria: acá nace la noticia (comunicados institucionales,
#           registros oficiales, revistas científicas).
# Nivel 2 = reporteo original: alguien verifica, llama, agrega contexto.
# Nivel 3 = reescritura: reproduce material ajeno sin agregar nada.
#
# Las listas son de fragmentos: basta que el nombre del medio contenga el texto.
MEDIOS_NIVEL_1 = [
    "nyu langone", "mass general", "massgeneral", "johns hopkins",
    "university of maryland", "uab", "nih", "fda", "who", "oms",
    "business wire", "businesswire", "pr newswire", "prnewswire",
    "egenesis", "united therapeutics", "clinicaltrials.gov", "europe pmc",
    "uspto", "harvard", "incucai", "uba", "unsam", "conicet",
]

MEDIOS_NIVEL_2 = [
    "stat", "endpoints", "fierce", "technology review", "npr", "nature",
    "science", "new york times", "nytimes", "washington post", "reuters",
    "associated press", "ap news", "bloomberg", "wall street journal",
    "financial times", "the guardian", "bbc", "el país", "el pais",
    "la nación", "la nacion", "clarín", "clarin", "infobae", "japan times",
    "scientific american", "new scientist", "der spiegel", "le monde",
    "página/12", "pagina 12", "el mercurio", "la tercera",
]

# Medios que ya demostraron no aportar. No se recolectan.
MEDIOS_EXCLUIDOS = [
    # terminales de datos financieros y análisis bursátil automatizado
    "simplywall", "marketscreener", "tradingview", "zacks", "insider monkey",
    "motley fool", "benzinga", "stocktwits", "investing.com", "gurufocus",
    # consultoras de informes de mercado
    "future market insights", "market research", "marketwatch press",
    "grand view research", "precedence research", "researchandmarkets",
    "openpr", "einpresswire", "globenewswire",
    # agregadores sin reporteo propio
    "dagens.com", "msn.com", "yahoo.com", "news18", "opoyi", "newsbreak",
]

# --- Umbral de relevancia ----------------------------------------------------
# Claude puntúa cada nota de 1 a 5. Se incluyen las que superan este número.
# 2 = todo lo que no sea ruido entra al informe como artículo. El informe ya no
# tiene sección de menciones breves: cada ítem se lee y se resume a mano, así
# que el umbral sólo separa material de ruido.
UMBRAL_RELEVANCIA = 2

# --- Categorías del informe --------------------------------------------------
# El orden acá es el orden en que aparecen las secciones en el PDF.
CATEGORIAS = [
    "Hitos clínicos",
    "Ciencia y preprints",
    "Ensayos clínicos",
    "Regulatorio",
    "Industria y financiamiento",
    "Patentes",
    "Bioética y opinión pública",
    "Región (América Latina)",
    "Otros",
]

# Sección de menciones breves: desactivada. Antes los ítems de puntaje 2 iban
# al final del informe como lista sin resumen; ahora entran como artículos
# normales (ver UMBRAL_RELEVANCIA) y se leen igual que el resto.
INCLUIR_MENCIONES_BREVES = False


# ==============================================================================
#  A PARTIR DE ACA NO HACE FALTA TOCAR NADA
# ==============================================================================

# ------------------------------------------------------------------ utilidades

def normalizar(texto):
    """Deja un título en minúsculas, sin tildes ni puntuación, para comparar."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9 ]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def parecidos(a, b, umbral=0.82):
    """True si dos títulos son básicamente la misma noticia."""
    return SequenceMatcher(None, a, b).ratio() >= umbral


def limpiar_titulo(titulo, fuente=""):
    """
    Google News agrega ' - Nombre del Medio' al final del titular.
    Se saca por coincidencia exacta con el nombre de la fuente, no por
    patrón: un titular que ya contiene un guion se cortaba de más.
    """
    t = (titulo or "").strip()
    if fuente:
        sufijo = f" - {fuente}"
        if t.endswith(sufijo):
            return t[: -len(sufijo)].strip()
    # Último recurso: sólo si queda un titular de largo razonable.
    recortado = re.sub(r"\s+-\s+[^-]{2,40}$", "", t).strip()
    return recortado if len(recortado) >= 25 else t


def limpiar_html(texto):
    """Saca etiquetas HTML de los resúmenes que vienen en el RSS."""
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = texto.replace("&nbsp;", " ").replace("&amp;", "&")
    texto = texto.replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", texto).strip()


# ------------------------------------------------------------- base de datos

def abrir_base():
    os.makedirs(CARPETA_SALIDA, exist_ok=True)
    con = sqlite3.connect(os.path.join(CARPETA_SALIDA, "vistos.db"))
    con.execute("""
        CREATE TABLE IF NOT EXISTS vistos (
            huella   TEXT PRIMARY KEY,
            titulo   TEXT,
            fuente   TEXT,
            url      TEXT,
            fecha    TEXT
        )
    """)
    con.commit()
    return con


def ya_visto(con, huella):
    cur = con.execute("SELECT 1 FROM vistos WHERE huella = ?", (huella,))
    return cur.fetchone() is not None


def marcar_visto(con, item):
    con.execute(
        "INSERT OR IGNORE INTO vistos VALUES (?, ?, ?, ?, ?)",
        (item["huella"], item["titulo"], item["fuente"],
         item["url"], item["fecha"].isoformat()),
    )


# ---------------------------------------------------------------- recolección

# Google News devuelve feeds vacíos a los clientes que se identifican como
# lector de RSS genérico. Hay que presentarse como un navegador.
NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/125.0.0.0 Safari/537.36")


def url_google_news(consulta, idioma, tramo=None):
    """Arma la URL del feed RSS de Google News para una búsqueda."""
    # Google News acota mejor por su propio operador when: que filtrando
    # después por fecha del lado nuestro.
    if tramo:
        desde, hasta = tramo
        filtro = f"after:{desde.date().isoformat()} before:{hasta.date().isoformat()}"
    else:
        filtro = f"when:{VENTANA_DIAS}d"

    q = urllib.parse.quote(f"{consulta} {filtro}")
    if idioma == "es":
        return (f"https://news.google.com/rss/search?q={q}"
                f"&hl=es-419&gl=AR&ceid=AR:es-419")
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl=en-US&gl=US&ceid=US:en")


def tramos_de_ventana():
    """
    Parte la ventana en tramos de TRAMO_DIAS.

    Google News devuelve un tope de resultados por consulta, y con ventanas
    largas ese tope se llena con lo más reciente y el resto se pierde. Cuatro
    consultas de una semana traen bastante más que una sola de un mes, porque
    cada una tiene su propio tope.

    Con ventanas cortas no hace falta: devuelve [None] y se usa when:Nd.
    """
    if not TRAMO_DIAS or VENTANA_DIAS <= TRAMO_DIAS:
        return [None]

    ahora = datetime.now(timezone.utc)
    tramos = []
    fin = ahora + timedelta(days=1)      # margen: before: es exclusivo
    restantes = VENTANA_DIAS
    while restantes > 0:
        paso = min(TRAMO_DIAS, restantes)
        inicio = fin - timedelta(days=paso + 1)
        tramos.append((inicio, fin))
        fin = inicio + timedelta(days=1)
        restantes -= paso
    return tramos


def recolectar_google_news():
    """Recorre todas las búsquedas y devuelve una lista plana de noticias."""
    corte = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS)
    resultados = []

    trabajos = ([(c, "es", False) for c in BUSQUEDAS_ES] +
                [(c, "en", False) for c in BUSQUEDAS_EN] +
                [(c, "en", False) for c in BUSQUEDAS_ACTORES])

    if INCLUIR_BUSQUEDAS_SITIOS:
        trabajos += [(f"site:{d} ({TERMINOS_SITIOS})", "en", True)
                     for d in SITIOS_PRIORITARIOS]

    tramos = tramos_de_ventana()
    if len(tramos) > 1:
        print(f"  (ventana partida en {len(tramos)} tramos de "
              f"{TRAMO_DIAS} días)")

    descartados_tema = 0

    for consulta, idioma, filtrar in trabajos:
        antes = len(resultados)
        fuera = 0
        entradas = []
        for tramo in tramos:
            url = url_google_news(consulta, idioma, tramo)
            try:
                feed = feedparser.parse(url, agent=NAVEGADOR)
            except Exception as e:
                print(f"  {consulta[:44]:<46} ERROR: {e}")
                continue
            entradas.extend(feed.entries)
            if len(tramos) > 1:
                time.sleep(0.4)      # no atropellar a Google

        for entrada in entradas:
            fecha = None
            if getattr(entrada, "published_parsed", None):
                fecha = datetime(*entrada.published_parsed[:6],
                                 tzinfo=timezone.utc)
            if fecha is None or fecha < corte:
                continue

            fuente = ""
            if getattr(entrada, "source", None):
                fuente = entrada.source.get("title", "")
            if not fuente:
                m = re.search(r"-\s+([^-]{2,40})$", entrada.get("title", ""))
                fuente = m.group(1).strip() if m else "Desconocido"

            titulo = limpiar_titulo(entrada.get("title", ""), fuente)
            extracto = limpiar_html(entrada.get("summary", ""))[:600]

            # En las búsquedas site: Google ignora con frecuencia el grupo
            # de términos y devuelve lo último publicado por el sitio, sea
            # del tema o no. Por eso se vuelve a filtrar acá.
            if filtrar:
                texto = (titulo + " " + extracto).lower()
                if not any(p in texto for p in FILTRO_FEEDS):
                    fuera += 1
                    continue

            resultados.append({
                "titulo": titulo,
                "fuente": fuente,
                "url": entrada.get("link", ""),
                "fecha": fecha,
                "extracto": extracto,
                "tipo": "prensa",
            })

        descartados_tema += fuera
        traidos = len(resultados) - antes
        aviso = "   <-- sin resultados" if traidos == 0 else ""
        if fuera:
            aviso = f"   ({fuera} fuera de tema)"
        # Las consultas por sitio son larguísimas; en el log va sólo el sitio.
        etiqueta = consulta
        if etiqueta.startswith("site:"):
            etiqueta = "sitio: " + etiqueta[5:].split(" ")[0]
        print(f"  {etiqueta[:44]:<46}{traidos:>4}{aviso}")

        time.sleep(1.5)  # cortesía con el servidor de Google

    if descartados_tema:
        print(f"  ({descartados_tema} resultados de búsquedas por sitio "
              f"descartados por no mencionar el tema)")

    return resultados


def dominio(url):
    """Devuelve el dominio de una URL, sin 'www.'."""
    try:
        d = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def nivel_de(item):
    """
    Nivel de la fuente: 1 primaria, 2 reporteo original, 3 reescritura o
    desconocida.

    Se resuelve en dos pasos porque las dos vías tienen puntos ciegos
    distintos:

      1. Por dominio de la URL. Es lo más confiable, pero NO sirve para lo
         que viene de Google News: esos enlaces apuntan todos a
         news.google.com y ocultan el medio real.
      2. Por nombre del medio. Es lo único disponible en el caso anterior,
         aunque es más frágil (dos medios pueden llamarse parecido).

    Los ítems que no vienen de prensa son primarios por definición: un
    registro de ensayos o un paper son la fuente misma.
    """
    if item.get("tipo") in ("paper", "preprint", "ensayo", "patente"):
        return 1

    d = dominio(item.get("url", ""))
    if d and "news.google" not in d:
        for clave, n in NIVEL_POR_DOMINIO.items():
            if d == clave or d.endswith("." + clave):
                return n

    f = normalizar(item.get("fuente", ""))
    if f:
        if any(normalizar(x) in f for x in MEDIOS_NIVEL_1):
            return 1
        if any(normalizar(x) in f for x in MEDIOS_NIVEL_2):
            return 2

    return 3


def esta_excluido(item):
    """True si la fuente ya demostró no aportar. Chequea dominio y nombre."""
    d = dominio(item.get("url", ""))
    if d and any(x in d for x in DOMINIOS_EXCLUIDOS):
        return True
    f = normalizar(item.get("fuente", ""))
    return bool(f) and any(normalizar(x) in f for x in MEDIOS_EXCLUIDOS)


ETIQUETA_NIVEL = {
    1: "fuente primaria",
    2: "reporteo original",
    3: "reescritura / sin verificar",
}


def recolectar_feeds_directos():
    """
    Lee los feeds de medios especializados e instituciones, y se queda con
    lo que menciona el tema. Corrige el sesgo de Google News hacia las
    fuentes de menor valor.
    """
    if not INCLUIR_FEEDS_DIRECTOS:
        return []

    corte = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS)
    salida = []

    for nombre, url, nivel in FEEDS_DIRECTOS:
        try:
            feed = feedparser.parse(url, agent=NAVEGADOR)
        except Exception as e:
            print(f"  {nombre[:30]:<32} ERROR: {e}")
            continue

        pertinentes = 0
        for e in feed.entries:
            fecha = None
            if getattr(e, "published_parsed", None):
                fecha = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            elif getattr(e, "updated_parsed", None):
                fecha = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
            if fecha is None or fecha < corte:
                continue

            titulo = limpiar_html(e.get("title", ""))
            extracto = limpiar_html(e.get("summary", ""))[:600]
            texto = (titulo + " " + extracto).lower()

            if not any(p in texto for p in FILTRO_FEEDS):
                continue

            salida.append({
                "titulo": titulo,
                "fuente": nombre,
                "url": e.get("link", ""),
                "fecha": fecha,
                "extracto": extracto,
                "tipo": "prensa",
                "nivel": nivel,
            })
            pertinentes += 1

        total = len(feed.entries)
        aviso = "   <-- feed vacío o caído" if total == 0 else ""
        print(f"  {nombre[:30]:<32}{pertinentes:>4} de {total:<4}{aviso}")
        time.sleep(0.5)

    return salida


def _traer_json(url, cabeceras=None):
    pedido = urllib.request.Request(url, headers=cabeceras or {})
    with urllib.request.urlopen(pedido, timeout=40) as r:
        return json.load(r)


TIPOS_MENORES = [
    "erratum", "errata", "correction", "corrigendum", "retraction",
    "retracted", "comment", "editorial", "letter", "reply",
    "author correction", "expression of concern",
]


def es_publicacion_menor(r):
    """
    True si el resultado de Europe PMC es una errata, corrección, comentario,
    carta o retractación en lugar de un trabajo propiamente dicho.

    Se chequea por dos vías porque ninguna es completa: el campo pubTypeList,
    que a veces viene vacío, y el título, que en estos casos casi siempre
    arranca con la palabra delatora.
    """
    tipos = (r.get("pubTypeList") or {}).get("pubType") or []
    if isinstance(tipos, str):
        tipos = [tipos]
    for t in tipos:
        if any(m in str(t).lower() for m in TIPOS_MENORES):
            return True

    titulo = (r.get("title") or "").lower().lstrip("[ ")
    inicios = ("correction", "erratum", "corrigendum", "retraction",
               "retracted", "comment on", "comment to", "author correction",
               "expression of concern", "reply to", "response to")
    return titulo.startswith(inicios)


def recolectar_ciencia():
    """Papers y preprints vía Europe PMC (indexa PubMed + bioRxiv + medRxiv)."""
    if not INCLUIR_CIENCIA:
        return []

    print("  consultando: Europe PMC (papers y preprints)")
    hoy = datetime.now(timezone.utc)
    desde = (hoy - timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%d")
    hasta = hoy.strftime("%Y-%m-%d")

    def pedir(consulta, etiqueta):
        url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
               + urllib.parse.urlencode({
                   "query": consulta, "format": "json",
                   "pageSize": MAX_CIENCIA, "resultType": "core",
                   "sort": "P_PDATE_D desc",
               }))
        try:
            d = _traer_json(url)
            res = d.get("resultList", {}).get("result", [])
            print(f"    {etiqueta:<24}{len(res):>4}")
            return res
        except Exception as e:
            print(f"    ERROR en Europe PMC ({etiqueta}): {e}")
            return []

    # Dos consultas separadas: si van juntas, los papers publicados desplazan
    # a los preprints y estos nunca aparecen.
    base = f'{CONSULTA_CIENCIA} AND (FIRST_PDATE:[{desde} TO {hasta}])'
    crudos = (pedir(base, "publicados")
              + pedir(base + " AND (SRC:PPR)", "preprints"))

    salida = []
    vistos_ids = set()
    descartados_tipo = 0
    for r in crudos:
        if r.get("id") in vistos_ids:
            continue
        vistos_ids.add(r.get("id"))

        # Europe PMC devuelve erratas, correcciones, cartas al editor y
        # retracciones con la misma jerarquía que un paper. Tienen DOI, así
        # que entran marcadas como fuente primaria, pero no son noticia: el
        # aviso de errata casi nunca dice qué corrige.
        if es_publicacion_menor(r):
            descartados_tipo += 1
            continue

        es_preprint = r.get("source") == "PPR"
        pid = r.get("pmid") or r.get("id", "")
        if r.get("doi"):
            enlace = f"https://doi.org/{r['doi']}"
        elif r.get("pmid"):
            enlace = f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/"
        else:
            enlace = f"https://europepmc.org/article/{r.get('source','MED')}/{pid}"

        try:
            fecha = datetime.strptime(r.get("firstPublicationDate", "")[:10],
                                      "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            fecha = datetime.now(timezone.utc)

        resumen = (r.get("abstractText") or "")[:800]
        salida.append({
            "titulo": (r.get("title") or "").rstrip("."),
            "fuente": r.get("journalTitle") or ("Preprint" if es_preprint
                                                else "Europe PMC"),
            "url": enlace,
            "fecha": fecha,
            "extracto": limpiar_html(resumen) or r.get("authorString", "")[:300],
            "tipo": "preprint" if es_preprint else "paper",
        })

    if descartados_tipo:
        print(f"    {'erratas y comentarios':<24}{descartados_tipo:>4} "
              f"descartados")

    return salida


def recolectar_ensayos():
    """Ensayos nuevos o actualizados en ClinicalTrials.gov (API v2, sin clave)."""
    if not INCLUIR_ENSAYOS:
        return []

    print("  consultando: ClinicalTrials.gov")
    desde = (datetime.now(timezone.utc)
             - timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%d")

    url = ("https://clinicaltrials.gov/api/v2/studies?"
           + urllib.parse.urlencode({
               "query.term": CONSULTA_ENSAYOS,
               "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{desde},MAX]",
               "pageSize": 40,
               "format": "json",
           }))

    try:
        datos = _traer_json(url)
    except Exception as e:
        print(f"    ERROR en ClinicalTrials.gov: {e}")
        return []

    # El buscador del registro expande los términos con sinónimos por su
    # cuenta y reintroduce "xenograft" en sentido oncológico y odontológico.
    # Por eso se filtra sobre el resultado, exigiendo mención porcina.
    TERMINOS_PORCINOS = ("pig", "porcine", "swine", "xenotransplant",
                         "xenokidney", "xenoheart", "cerdo")

    salida = []
    descartados = 0
    for est in datos.get("studies", []):
        p = est.get("protocolSection", {})
        ident = p.get("identificationModule", {})
        estado = p.get("statusModule", {})
        patro = (p.get("sponsorCollaboratorsModule", {})
                  .get("leadSponsor", {}).get("name", ""))
        nct = ident.get("nctId", "")

        try:
            fecha = datetime.strptime(
                estado.get("lastUpdatePostDateStruct", {}).get("date", "")[:10],
                "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            fecha = datetime.now(timezone.utc)

        resumen_est = (p.get("descriptionModule", {})
                        .get("briefSummary", ""))
        texto_est = normalizar(f"{ident.get('briefTitle','')} "
                               f"{ident.get('officialTitle','')} {resumen_est}")
        if not any(t in texto_est for t in TERMINOS_PORCINOS):
            descartados += 1
            continue

        fases = ", ".join(p.get("designModule", {}).get("phases", []) or [])
        salida.append({
            "titulo": ident.get("briefTitle", nct),
            "fuente": f"ClinicalTrials.gov · {patro}" if patro
                      else "ClinicalTrials.gov",
            "url": f"https://clinicaltrials.gov/study/{nct}",
            "fecha": fecha,
            "extracto": (f"{nct}. Estado: "
                         f"{estado.get('overallStatus','')}. "
                         f"Fase: {fases or 'no especificada'}. "
                         f"Patrocinante: {patro}."),
            "tipo": "ensayo",
        })

    if descartados:
        print(f"    {descartados} ensayos descartados por no ser del campo")

    return salida


def recolectar_patentes():
    """Patentes concedidas vía PatentsView. Se saltea si no hay clave."""
    if not CLAVE_PATENTES:
        print("  (patentes: sin clave configurada, se omite)")
        return []

    print("  consultando: PatentsView (patentes)")
    desde = (datetime.now(timezone.utc)
             - timedelta(days=max(VENTANA_DIAS, 30))).strftime("%Y-%m-%d")

    q = json.dumps({"_and": [
        {"_gte": {"patent_date": desde}},
        {"_text_any": {"patent_title": CONSULTA_PATENTES}},
    ]})
    f = json.dumps(["patent_id", "patent_title", "patent_date",
                    "patent_abstract"])
    url = ("https://search.patentsview.org/api/v1/patent/?"
           + urllib.parse.urlencode({"q": q, "f": f,
                                     "o": json.dumps({"size": 25})}))

    try:
        datos = _traer_json(url, {"X-Api-Key": CLAVE_PATENTES})
    except Exception as e:
        print(f"    ERROR en PatentsView: {e}")
        return []

    salida = []
    for p in datos.get("patents", []) or []:
        try:
            fecha = datetime.strptime(p.get("patent_date", "")[:10],
                                      "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            fecha = datetime.now(timezone.utc)
        salida.append({
            "titulo": p.get("patent_title", ""),
            "fuente": "USPTO",
            "url": f"https://patents.google.com/patent/US{p.get('patent_id','')}",
            "fecha": fecha,
            "extracto": (p.get("patent_abstract") or "")[:600],
            "tipo": "patente",
        })

    return salida



# ------------------------------------------------------------- deduplicación

def deduplicar(items):
    """
    Colapsa la misma noticia replicada en varios medios.
    Se queda con la primera aparición y anota cuántas veces se repitió.
    """
    unicos = []
    for item in sorted(items, key=lambda x: x["fecha"], reverse=True):
        clave = normalizar(item["titulo"])
        if not clave:
            continue
        if esta_excluido(item):
            continue
        item["huella"] = clave[:120]
        item["nivel"] = nivel_de(item)

        encontrado = False
        for u in unicos:
            if parecidos(clave, normalizar(u["titulo"])):
                u["replicas"] = u.get("replicas", 1) + 1
                # Si la réplica viene de mejor fuente, nos quedamos con esa:
                # la misma noticia contada por STAT vale más que por un
                # agregador.
                if nivel_de(item) < nivel_de(u):
                    for c in ("titulo", "fuente", "url", "extracto", "nivel"):
                        if c in item:
                            u[c] = item[c]
                encontrado = True
                break
        if not encontrado:
            item["replicas"] = 1
            unicos.append(item)

    return unicos


# ------------------------------------------------- clasificación con Claude

INSTRUCCIONES = """Sos un analista de un servicio de monitoreo especializado \
en xenotrasplantes: el trasplante de órganos, tejidos o células de animales \
—principalmente cerdos modificados genéticamente— a seres humanos.

Recibís ítems de cinco orígenes distintos, indicados en el campo "tipo":
prensa, paper, preprint, ensayo (registro de ensayo clínico) y patente.

Para cada ítem devolvé:

- "id": el mismo número que recibiste.

- "relevancia": entero de 1 a 5.
    5 = hito del campo. Primer trasplante de un órgano nuevo, muerte o rechazo
        de un paciente en ensayo, aprobación o freno regulatorio de peso,
        resultado clínico publicado en revista de primera línea.
    4 = novedad sustantiva. Avance científico con resultados, ensayo nuevo o
        cambio de fase, ronda de inversión o acuerdo, posición de un regulador,
        patente sobre una línea porcina o técnica central, desarrollo regional.
    3 = aporte útil pero secundario. Revisiones, explicativos de medios serios,
        entrevistas a referentes, preprints incrementales, actualizaciones
        menores de un ensayo ya conocido.
    2 = mención tangencial. Toca el tema al pasar o repite algo ya sabido.
    1 = ruido. No es sobre xenotrasplantes: trasplantes humano-humano,
        porcinocultura, publicidad, o coincidencia de palabras.

  Los ítems de tipo "ensayo" y "patente" rara vez bajan de 3: aunque el titular
  sea árido, son señal temprana y el usuario los quiere ver.

  Cada ítem trae un campo "nivel": 1 es fuente primaria (comunicado
  institucional, registro oficial, revista científica), 2 es reporteo original
  de un medio que verifica y agrega contexto, 3 es reescritura de material
  ajeno. Usalo así:
    - Nunca pongas 5 a un ítem de nivel 3. Un hito real lo publica primero una
      fuente primaria o un medio de reporteo original; si sólo lo trae un sitio
      de reescritura, es contenido reciclado o mal fechado, y va como máximo 3.
    - Ante la duda entre dos puntajes, el nivel 1 o 2 sube y el 3 baja.

  Bajá a 2 lo que sea sobre la cotización bursátil, resultados trimestrales o
  gobierno corporativo de una empresa del rubro: es información financiera, no
  del campo. Bajá también a 2 los estudios de biología porcina general (genómica
  del cerdo, fisiología, producción animal) que no tengan relación explícita con
  trasplante a humanos.

- "categoria": exactamente una de estas etiquetas:
    "Hitos clínicos" — procedimientos en pacientes, resultados, supervivencia.
    "Ciencia y preprints" — investigación básica, edición génica, inmunología,
        zoonosis (PERV, pCMV), modelos animales.
    "Ensayos clínicos" — todo ítem de tipo "ensayo".
    "Regulatorio" — FDA, EMA, ANMAT, INCUCAI, OMS, marcos legales, bioseguridad.
    "Industria y financiamiento" — empresas, inversiones, alianzas, capacidad
        productiva, personal directivo.
    "Patentes" — todo ítem de tipo "patente".
    "Bioética y opinión pública" — ética, religión, aceptación social, debate
        sobre justicia en el acceso, bienestar animal.
    "Región (América Latina)" — cuando el eje sea Argentina o la región,
        aunque el contenido también sea científico o regulatorio.
    "Otros" — lo que no encaje.

- "resumen": dos oraciones en español neutro, redactadas con tus propias
    palabras, que digan QUÉ pasó y POR QUÉ importa para alguien que sigue el
    campo. No copies frases del titular ni del extracto: parafraseá.
    Si la relevancia es 1, devolvé una cadena vacía.

Devolvé ÚNICAMENTE un array JSON. Sin explicaciones, sin markdown, sin ```."""


def clasificar(items, cliente):
    """
    Manda los ítems a Claude en tandas.

    Devuelve tres listas: los que pasan el umbral, las menciones breves (hoy
    desactivadas) y los DESCARTADOS. Los descartados se devuelven en lugar de
    perderse porque el criterio de ruido lo aplica un modelo leyendo sólo el
    titular y un extracto, que es justamente el error que el informe existe
    para corregir. Si algo se clasificó mal, tiene que poder auditarse.
    """
    aprobados = []
    menciones = []
    descartados = []
    TANDA = 12

    for inicio in range(0, len(items), TANDA):
        tanda = items[inicio:inicio + TANDA]
        payload = [
            {
                "id": i,
                "titulo": it["titulo"],
                "fuente": it["fuente"],
                "fecha": it["fecha"].strftime("%Y-%m-%d"),
                "nivel_fuente": nivel_de(it),
                "extracto": it["extracto"],
            }
            for i, it in enumerate(tanda)
        ]

        print(f"  clasificando {inicio + 1}-{inicio + len(tanda)} "
              f"de {len(items)}")

        try:
            r = cliente.messages.create(
                model=MODELO,
                max_tokens=4000,
                system=INSTRUCCIONES,
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }],
            )
            texto = "".join(b.text for b in r.content if b.type == "text")
            texto = re.sub(r"^```(?:json)?|```$", "", texto.strip(),
                           flags=re.MULTILINE).strip()
            veredictos = json.loads(texto)
        except Exception as e:
            print(f"    ERROR al clasificar: {e}")
            continue

        for v in veredictos:
            idx = v.get("id")
            if not isinstance(idx, int) or idx >= len(tanda):
                continue
            puntaje = v.get("relevancia", 0)
            item = dict(tanda[idx])
            item["relevancia"] = puntaje
            item["categoria"] = v.get("categoria", "Otros")
            item["resumen"] = v.get("resumen", "")

            if puntaje >= UMBRAL_RELEVANCIA:
                aprobados.append(item)
            elif puntaje == 2 and INCLUIR_MENCIONES_BREVES:
                menciones.append(item)
            else:
                descartados.append(item)

        time.sleep(1)

    return aprobados, menciones, descartados


def marcar_desde_json(ruta):
    """
    Marca como vistos los ítems de un seleccion_*.json ya usado en un informe.

    Se usa después de --sin-triaje: como ahí no se marca nada al recolectar
    (el triaje todavía no había ocurrido), este paso cierra el círculo cuando
    el informe ya está hecho. Sin esto, la semana siguiente volvería a traer
    todo lo de esta.

    La huella es el título normalizado, igual que en la recolección, así que
    alcanza con el JSON: no hace falta volver a recolectar.
    """
    if not os.path.exists(ruta):
        print(f"No encuentro el archivo: {ruta}")
        return

    with open(ruta, encoding="utf-8") as f:
        datos = json.load(f)

    filas = datos.get("seleccion", []) + datos.get("menciones", [])
    filas += datos.get("descartados", [])
    if not filas:
        print("El archivo no tiene ítems.")
        return

    con = abrir_base()
    marcados = 0
    for fila in filas:
        clave = normalizar(fila.get("titulo", ""))
        if not clave:
            continue
        con.execute(
            "INSERT OR IGNORE INTO vistos VALUES (?, ?, ?, ?, ?)",
            (clave[:120], fila.get("titulo", ""), fila.get("fuente", ""),
             fila.get("url", ""), fila.get("fecha", "")),
        )
        marcados += 1
    con.commit()
    con.close()
    print(f"{marcados} ítems marcados como vistos desde {ruta}")


def sin_clasificar(items):
    """
    Modo sin API: devuelve todo el material recolectado tal cual, sin puntuar
    ni clasificar.

    El triaje pasa a hacerlo quien arma el informe, que además puede abrir la
    nota antes de decidir —el triaje automático puntúa leyendo sólo el titular
    y un extracto, así que este modo no es un downgrade: mueve la decisión a
    donde hay más información.

    La categoría queda vacía a propósito: se asigna al escribir el informe.
    """
    aprobados = []
    for it in items:
        item = dict(it)
        item["relevancia"] = 0          # 0 = sin puntuar
        item["categoria"] = "Sin clasificar"
        item["resumen"] = ""
        aprobados.append(item)
    return aprobados, [], []


# --------------------------------------------------------------------- PDF

def estilos():
    s = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "T", parent=s["Title"], fontSize=20, leading=24,
            textColor=colors.HexColor("#1a1a1a"), spaceAfter=2),
        "bajada": ParagraphStyle(
            "B", parent=s["Normal"], fontSize=10, leading=13,
            textColor=colors.HexColor("#666666"), spaceAfter=18),
        "seccion": ParagraphStyle(
            "S", parent=s["Heading2"], fontSize=13, leading=16,
            textColor=colors.HexColor("#1a4d8f"),
            spaceBefore=16, spaceAfter=8),
        "nota": ParagraphStyle(
            "N", parent=s["Normal"], fontSize=10.5, leading=14,
            spaceAfter=3),
        "meta": ParagraphStyle(
            "M", parent=s["Normal"], fontSize=8.5, leading=11,
            textColor=colors.HexColor("#777777"), spaceAfter=10),
        "vacio": ParagraphStyle(
            "V", parent=s["Normal"], fontSize=10, leading=13,
            textColor=colors.HexColor("#999999")),
    }


def escapar(t):
    """Escapa caracteres que reportlab interpreta como marcado."""
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generar_pdf(items, menciones, ruta):
    st = estilos()
    doc = SimpleDocTemplate(
        ruta, pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="Monitoreo de xenotrasplantes",
    )

    hoy = datetime.now()
    desde = hoy - timedelta(days=VENTANA_DIAS)

    historia = [
        Paragraph("Monitoreo de xenotrasplantes", st["titulo"]),
        Paragraph(
            f"Período {desde.strftime('%d/%m/%Y')} – "
            f"{hoy.strftime('%d/%m/%Y')} &nbsp;·&nbsp; "
            f"{len(items)} ítems seleccionados"
            + (f" · {len(menciones)} menciones breves" if menciones else ""),
            st["bajada"]),
        HRFlowable(width="100%", thickness=1,
                   color=colors.HexColor("#dddddd")),
    ]

    if not items and not menciones:
        historia.append(Spacer(1, 20))
        historia.append(Paragraph(
            "No se registraron novedades relevantes en este período.",
            st["vacio"]))
        doc.build(historia)
        return

    for categoria in CATEGORIAS:
        grupo = [i for i in items if i.get("categoria") == categoria]
        if not grupo:
            continue

        grupo.sort(key=lambda x: (-x.get("relevancia", 0), x["fecha"]))

        historia.append(Paragraph(categoria.upper(), st["seccion"]))

        for it in grupo:
            marca = ('<font color="#c0392b"> [HITO]</font>'
                     if it.get("relevancia", 0) >= 5 else "")
            replicas = it.get("replicas", 1)
            eco = f" · {replicas} medios" if replicas > 2 else ""
            etiquetas = {"paper": " · paper", "preprint": " · preprint",
                         "ensayo": " · registro de ensayo",
                         "patente": " · patente"}
            tipo = etiquetas.get(it.get("tipo"), "")

            nivel = it.get("nivel", 3)
            if nivel == 3:
                sello = ('<font color="#b06a00"> · '
                         + ETIQUETA_NIVEL[3] + "</font>")
            else:
                sello = f" · {ETIQUETA_NIVEL[nivel]}"

            n = nivel_de(it)
            if it.get("tipo") == "prensa":
                color = {1: "#1e6f50", 2: "#1a4d8f"}.get(n, "#b03a2e")
                sello = (f' · <font color="{color}">{ETIQUETA_NIVEL[n]}</font>')
            else:
                sello = ""

            bloque = [
                Paragraph(f"<b>{escapar(it['titulo'])}</b>{marca}",
                          st["nota"]),
                Paragraph(escapar(it.get("resumen", "")), st["nota"]),
                Paragraph(
                    f"{escapar(it['fuente'])} · "
                    f"{it['fecha'].strftime('%d/%m/%Y')}{eco}{tipo}{sello} · "
                    f'<link href="{it["url"]}" color="#1a4d8f">ver nota</link>',
                    st["meta"]),
            ]
            historia.append(KeepTogether(bloque))

    if menciones:
        historia.append(Spacer(1, 10))
        historia.append(HRFlowable(width="100%", thickness=1,
                                   color=colors.HexColor("#dddddd")))
        historia.append(Paragraph("OTRAS MENCIONES", st["seccion"]))
        historia.append(Paragraph(
            "Ítems de relevancia menor, listados sin resumen para que nada "
            "quede fuera del radar.", st["vacio"]))
        historia.append(Spacer(1, 6))
        for m in sorted(menciones, key=lambda x: x["fecha"], reverse=True):
            historia.append(Paragraph(
                f'{escapar(m["titulo"])} — '
                f'<font color="#777777">{escapar(m["fuente"])}, '
                f'{m["fecha"].strftime("%d/%m")}</font> '
                f'<link href="{m["url"]}" color="#1a4d8f">→</link>',
                st["meta"]))

    doc.build(historia)


# ------------------------------------------------------------------- programa

def exportar_archivo_historico(con, ruta):
    """
    Vuelca toda la base acumulada a un CSV. Con el tiempo esto se vuelve el
    archivo histórico del tema: buscable, ordenable, y semilla de cualquier
    base de conocimiento que quieran armar después.
    """
    import csv
    filas = con.execute(
        "SELECT fecha, titulo, fuente, url FROM vistos ORDER BY fecha DESC"
    ).fetchall()
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fecha", "titulo", "fuente", "url"])
        w.writerows(filas)
    return len(filas)


def exportar_seleccion(items, menciones, descartados, ruta, dias):
    """
    Vuelca lo que pasó el triage a un JSON, para que la capa editorial
    (la skill de Cowork) lo tome desde ahí.

    Este archivo es el punto de entrega del script: hasta acá llega la
    máquina —recolectar, deduplicar, puntuar—, y de acá en adelante se
    leen las notas y se redacta.

    El campo "resumen_previo" es el resumen automático hecho a partir del
    titular y el extracto. NO es el resumen final: sirve de referencia
    para saber de qué se trata la nota antes de abrirla.

    La lista "descartados" guarda lo que el triaje consideró ruido
    (relevancia 1). No va al informe, pero queda registrado para poder
    auditarlo: el triaje puntúa leyendo sólo el titular y un extracto, así
    que a veces se equivoca, y sin este registro el error sería invisible.
    """
    hasta = datetime.now()
    desde = hasta - timedelta(days=dias)

    def fila(it):
        return {
            "titulo": it.get("titulo", ""),
            "fuente": it.get("fuente", ""),
            "fecha": it["fecha"].strftime("%Y-%m-%d"),
            "url": it.get("url", ""),
            "tipo": it.get("tipo", "prensa"),
            "nivel": nivel_de(it),
            "etiqueta_nivel": ETIQUETA_NIVEL.get(nivel_de(it), ""),
            "relevancia": it.get("relevancia", 0),
            "categoria": it.get("categoria", "Otros"),
            "resumen_previo": it.get("resumen", ""),
            "extracto": (it.get("extracto", "") or "")[:600],
        }

    datos = {
        "generado": hasta.strftime("%Y-%m-%dT%H:%M"),
        "periodo": {"desde": desde.strftime("%Y-%m-%d"),
                    "hasta": hasta.strftime("%Y-%m-%d")},
        "orden_categorias": CATEGORIAS,
        "seleccion": [fila(i) for i in items],
        "menciones": [fila(i) for i in menciones],
        "descartados": [fila(i) for i in descartados],
    }

    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

    return (len(datos["seleccion"]), len(datos["menciones"]),
            len(datos["descartados"]))


def resumen_embudo(crudos, unicos, nuevos, informe, menciones):
    """
    Imprime dónde se angosta el embudo, por tipo de fuente.
    Sirve para saber si el problema es que no llega nada, o que se está
    filtrando de más.
    """
    tipos = ["prensa", "paper", "preprint", "ensayo", "patente"]

    def contar(lista, tipo):
        return sum(1 for i in lista if i.get("tipo") == tipo)

    en_informe = informe + menciones

    print("\n" + "=" * 66)
    print("RESUMEN DE LA CORRIDA")
    print("=" * 66)
    print(f"{'fuente':<12}{'crudos':>9}{'únicos':>9}{'nuevos':>9}"
          f"{'informe':>9}")
    print("-" * 66)
    for t in tipos:
        print(f"{t:<12}{contar(crudos, t):>9}{contar(unicos, t):>9}"
              f"{contar(nuevos, t):>9}{contar(en_informe, t):>9}")
    print("-" * 66)
    print(f"{'TOTAL':<12}{len(crudos):>9}{len(unicos):>9}"
          f"{len(nuevos):>9}{len(en_informe):>9}")
    print("=" * 66)

    # Lectura de lo que pasó, en castellano.
    perdidos_dup = len(crudos) - len(unicos)
    perdidos_vistos = len(unicos) - len(nuevos)
    perdidos_ruido = len(nuevos) - len(en_informe)

    # Composición por nivel ANTES de filtrar. Esta es la tabla que dice si las
    # fuentes buenas están entrando al sistema; la de abajo dice si llegan al
    # informe, que depende además de la relevancia y de la base de vistos.
    prensa_cruda = [i for i in unicos if i.get("tipo") == "prensa"]
    if prensa_cruda:
        print("\nNivel de TODA la prensa recolectada (antes de filtrar):")
        for n in (1, 2, 3):
            c = sum(1 for i in prensa_cruda if nivel_de(i) == n)
            print(f"  {n} · {ETIQUETA_NIVEL[n]:<26}{c:>4}  {'#' * min(c, 40)}")

    # Composición por nivel de fuente de lo que llegó al informe.
    prensa_informe = [i for i in en_informe if i.get("tipo") == "prensa"]
    if prensa_informe:
        print("\nCalidad de las fuentes de prensa del informe:")
        for n in (1, 2, 3):
            c = sum(1 for i in prensa_informe if nivel_de(i) == n)
            print(f"  {n} · {ETIQUETA_NIVEL[n]:<26}{c:>4}  {'#' * c}")
        n3 = sum(1 for i in prensa_informe if nivel_de(i) == 3)
        if n3 > len(prensa_informe) * 0.5:
            print("\n  → Más de la mitad de la prensa viene de reescritura.")
            print("    Revisá esos ítems antes de mandarlos: es de donde")
            print("    salen los falsos hitos (notas viejas recirculadas).")

    if prensa_cruda:
        n2_crudo = sum(1 for i in prensa_cruda if nivel_de(i) == 2)
        if n2_crudo == 0:
            print("\n  → No entró NADA de reporteo original (nivel 2).")
            print("    Es lo esperable en ventanas largas: los RSS de los")
            print("    medios grandes sólo guardan sus últimas notas y no")
            print("    llegan a cubrir la ventana. La prensa de calidad hay")
            print("    que buscarla en el barrido expandido, al armar el")
            print("    informe.")

    print("\nDónde se fue el material:")
    print(f"  {perdidos_dup:>5} descartados por duplicado "
          f"(la misma noticia en varios medios)")
    print(f"  {perdidos_vistos:>5} descartados por ya vistos "
          f"(salieron en informes anteriores)")
    print(f"  {perdidos_ruido:>5} descartados por relevancia 1 "
          f"(el triaje los consideró ruido)")
    if perdidos_ruido:
        print("        quedan listados en el JSON, campo 'descartados',")
        print("        por si el triaje se equivocó")

    if perdidos_vistos > len(nuevos) * 2 and perdidos_vistos > 10:
        print("\n  → La mayor parte se filtró por 'ya vistos'. Es el sistema")
        print("    funcionando: ya te llegaron. Para ver el panorama completo")
        print("    de un período, corré:  python xeno_clipping.py --rehacer")

    if contar(crudos, "prensa") < 60:
        print("\n  → Llegó poca prensa en bruto. Mirá el listado de búsquedas")
        print("    más arriba: si muchas dicen '<-- sin resultados', Google")
        print("    está devolviendo feeds vacíos. Esperá unos minutos y")
        print("    volvé a correrlo.")

    n3 = sum(1 for i in en_informe
             if i.get("tipo") == "prensa" and nivel_de(i) == 3)
    prensa_inf = sum(1 for i in en_informe if i.get("tipo") == "prensa")
    if prensa_inf and n3 > prensa_inf * 0.5:
        print("\n  → Más de la mitad de la prensa del informe es de")
        print("    reescritura. Revisá esos ítems antes de reenviarlos:")
        print("    es de donde salen los falsos hitos.")

    for t in ("paper", "ensayo"):
        n, ok = contar(nuevos, t), contar(en_informe, t)
        if n >= 10 and ok < n * 0.25:
            print(f"\n  → De {n} ítems de tipo '{t}', sólo {ok} pasaron el")
            print(f"    filtro. Eso indica que la consulta de esa fuente está")
            print(f"    trayendo material de otro campo. Revisá CONSULTA_"
                  f"{'CIENCIA' if t == 'paper' else 'ENSAYOS'}.")


def main():
    global VENTANA_DIAS

    ap = argparse.ArgumentParser(
        description="Clipping de xenotrasplantes")
    ap.add_argument("--dias", type=int, default=VENTANA_DIAS,
                    help="cuántos días hacia atrás mirar (por defecto 7)")
    ap.add_argument("--marcar-vistos", metavar="JSON", dest="marcar_vistos",
                    help="no recolecta: toma un seleccion_*.json ya usado en "
                         "un informe y marca sus ítems como vistos, para que "
                         "no vuelvan a aparecer. Se corre al terminar el "
                         "informe cuando se usó --sin-triaje")
    ap.add_argument("--sin-triaje", action="store_true", dest="sin_triaje",
                    help="no usa la API: exporta TODO lo recolectado sin "
                         "puntuar ni clasificar, para que el triaje lo haga "
                         "la sesión de Cowork al armar el informe")
    ap.add_argument("--rehacer", action="store_true",
                    help="ignora la base de vistos y NO la actualiza; "
                         "sirve para ver el panorama completo de un período "
                         "sin gastar el estado del sistema")
    args = ap.parse_args()
    VENTANA_DIAS = args.dias

    if args.marcar_vistos:
        marcar_desde_json(args.marcar_vistos)
        return

    # El triaje con la API es opcional. Sin clave, el script recolecta igual
    # y deja el material sin puntuar para que lo triee quien arma el informe.
    sin_triaje = args.sin_triaje or not API_KEY
    if sin_triaje and not args.sin_triaje:
        print("\nAviso: no hay ANTHROPIC_API_KEY en el entorno.")
        print("Sigo sin triaje automático: voy a recolectar todo y dejarlo")
        print("sin puntuar, para que el triaje lo haga quien arme el informe.")

    cliente = None if sin_triaje else Anthropic(api_key=API_KEY)
    con = abrir_base()

    modo = " (modo repaso: se ignora la base de vistos)" if args.rehacer else ""
    print(f"\nVentana: últimos {VENTANA_DIAS} días{modo}")

    print("\n[1/7] Recolectando prensa (Google News)...")
    crudos = recolectar_google_news()
    print(f"      {len(crudos)} resultados en bruto")

    print("\n[2/7] Recolectando medios especializados e instituciones...")
    directos = recolectar_feeds_directos()
    print(f"      {len(directos)} pertinentes")
    crudos += directos

    print("\n[3/7] Recolectando literatura científica y preprints...")
    ciencia = recolectar_ciencia()
    print(f"      {len(ciencia)} papers y preprints")

    print("\n[4/7] Recolectando ensayos y patentes...")
    ensayos = recolectar_ensayos()
    patentes = recolectar_patentes()
    print(f"      {len(ensayos)} ensayos · {len(patentes)} patentes")

    crudos += ciencia + ensayos + patentes

    print("\n[5/7] Filtrando, deduplicando y descartando ya vistos...")
    antes_excl = len(crudos)
    crudos = [i for i in crudos if not esta_excluido(i)]
    excluidos = antes_excl - len(crudos)
    if excluidos:
        print(f"      {excluidos} descartados por dominio excluido")
    unicos = deduplicar(crudos)
    if args.rehacer:
        nuevos = unicos
    else:
        nuevos = [i for i in unicos if not ya_visto(con, i["huella"])]
    print(f"      {len(unicos)} únicos · {len(nuevos)} a clasificar")

    if not nuevos:
        print("\nNo hay novedades. No se genera informe.")
        resumen_embudo(crudos, unicos, nuevos, [], [])
        con.close()
        return

    if sin_triaje:
        print("\n[6/7] Sin triaje automático: paso todo al informe.")
        seleccion, menciones, descartados = sin_clasificar(nuevos)
        print(f"      {len(seleccion)} ítems sin puntuar, para trienar al "
              f"armar el informe")
    else:
        print("\n[6/7] Clasificando y resumiendo...")
        seleccion, menciones, descartados = clasificar(nuevos, cliente)
        print(f"      {len(seleccion)} para el informe · "
              f"{len(descartados)} descartados por ruido")

    print("\n[7/7] Escribiendo la selección...")
    sello = datetime.now().strftime("%Y-%m-%d")
    sufijo = "_repaso" if args.rehacer else ""
    ruta_json = os.path.join(
        CARPETA_SALIDA, f"seleccion_{sello}{sufijo}.json")
    n_sel, n_men, n_desc = exportar_seleccion(
        seleccion, menciones, descartados, ruta_json, VENTANA_DIAS)
    print(f"      {n_sel} ítems a leer"
          + (f" · {n_men} menciones" if n_men else "")
          + f" · {n_desc} descartados quedaron registrados")

    # El PDF automático quedó reemplazado por el informe que se arma
    # después leyendo las notas. Si alguna vez querés el PDF viejo de
    # respaldo, descomentá estas dos líneas:
    # ruta_pdf = os.path.join(
    #     CARPETA_SALIDA, f"xenotrasplantes_{sello}{sufijo}.pdf")
    # generar_pdf(seleccion, menciones, ruta_pdf)

    if args.rehacer:
        pass
    elif sin_triaje:
        # En modo sin triaje NO se marca nada como visto. El triaje lo hace
        # después quien arma el informe, y si acá marcáramos todo, lo que
        # quede fuera de esta corrida se perdería sin que nadie lo haya
        # mirado. Se marca al final, desde la skill, con --marcar-vistos.
        print("\n      (no se marcó nada como visto: el triaje es posterior)")
    else:
        # Marcamos como vistos TODOS los nuevos, incluso el ruido: así
        # tampoco vuelve a aparecer la semana que viene.
        for i in nuevos:
            marcar_visto(con, i)
        con.commit()

    ruta_csv = os.path.join(CARPETA_SALIDA, "archivo_historico.csv")
    total = exportar_archivo_historico(con, ruta_csv)
    con.close()

    resumen_embudo(crudos, unicos, nuevos, seleccion, menciones)

    print(f"\nSelección: {ruta_json}")
    print(f"Archivo:   {ruta_csv}  ({total} ítems acumulados)")
    print("\nSiguiente paso: abrí Cowork en esta carpeta y pedí el informe.\n")


if __name__ == "__main__":
    main()
