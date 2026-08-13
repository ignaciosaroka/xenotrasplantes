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

# --- Dónde se guardan las cosas ----------------------------------------------
# Cambiá esta ruta por la carpeta que quieras usar.
CARPETA_SALIDA = r"C:\Users\nsaro\OneDrive\Desktop\Claude Raúl"

# --- Clave de la API de Anthropic --------------------------------------------
# Se lee de la variable de entorno ANTHROPIC_API_KEY.
# Si preferís, podés pegarla acá directamente entre las comillas, pero es
# mejor práctica usar la variable de entorno.
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

# --- Umbral de relevancia ----------------------------------------------------
# Claude puntúa cada nota de 1 a 5. Se incluyen las que superan este número.
# 3 = razonablemente inclusivo. Subilo a 4 si te llega demasiado ruido.
UMBRAL_RELEVANCIA = 3

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

# Los ítems que Claude puntúa en 2 no se descartan: van al final del informe
# como una lista de una línea, sin resumen. Así nada queda invisible.
INCLUIR_MENCIONES_BREVES = True


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


def url_google_news(consulta, idioma):
    """Arma la URL del feed RSS de Google News para una búsqueda."""
    # Google News acota mejor por su propio operador when: que filtrando
    # después por fecha del lado nuestro.
    q = urllib.parse.quote(f"{consulta} when:{VENTANA_DIAS}d")
    if idioma == "es":
        return (f"https://news.google.com/rss/search?q={q}"
                f"&hl=es-419&gl=AR&ceid=AR:es-419")
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl=en-US&gl=US&ceid=US:en")


def recolectar_google_news():
    """Recorre todas las búsquedas y devuelve una lista plana de noticias."""
    corte = datetime.now(timezone.utc) - timedelta(days=VENTANA_DIAS)
    resultados = []

    trabajos = ([(c, "es") for c in BUSQUEDAS_ES] +
                [(c, "en") for c in BUSQUEDAS_EN] +
                [(c, "en") for c in BUSQUEDAS_ACTORES])

    for consulta, idioma in trabajos:
        url = url_google_news(consulta, idioma)
        try:
            feed = feedparser.parse(url, agent=NAVEGADOR)
        except Exception as e:
            print(f"  {consulta[:44]:<46} ERROR: {e}")
            continue

        antes = len(resultados)
        for entrada in feed.entries:
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

            resultados.append({
                "titulo": limpiar_titulo(entrada.get("title", ""), fuente),
                "fuente": fuente,
                "url": entrada.get("link", ""),
                "fecha": fecha,
                "extracto": limpiar_html(entrada.get("summary", ""))[:600],
                "tipo": "prensa",
            })

        traidos = len(resultados) - antes
        aviso = "   <-- sin resultados" if traidos == 0 else ""
        print(f"  {consulta[:44]:<46}{traidos:>4}{aviso}")

        time.sleep(1.5)  # cortesía con el servidor de Google

    return resultados


def _traer_json(url, cabeceras=None):
    pedido = urllib.request.Request(url, headers=cabeceras or {})
    with urllib.request.urlopen(pedido, timeout=40) as r:
        return json.load(r)


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
    for r in crudos:
        if r.get("id") in vistos_ids:
            continue
        vistos_ids.add(r.get("id"))
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

    salida = []
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
        item["huella"] = clave[:120]

        encontrado = False
        for u in unicos:
            if parecidos(clave, normalizar(u["titulo"])):
                u["replicas"] = u.get("replicas", 1) + 1
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
    Si la relevancia es 1 o 2, devolvé una cadena vacía.

Devolvé ÚNICAMENTE un array JSON. Sin explicaciones, sin markdown, sin ```."""


def clasificar(items, cliente):
    """Manda los ítems a Claude en tandas y devuelve los que pasan el umbral."""
    aprobados = []
    menciones = []
    TANDA = 12

    for inicio in range(0, len(items), TANDA):
        tanda = items[inicio:inicio + TANDA]
        payload = [
            {
                "id": i,
                "titulo": it["titulo"],
                "fuente": it["fuente"],
                "fecha": it["fecha"].strftime("%Y-%m-%d"),
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

        time.sleep(1)

    return aprobados, menciones


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

            bloque = [
                Paragraph(f"<b>{escapar(it['titulo'])}</b>{marca}",
                          st["nota"]),
                Paragraph(escapar(it.get("resumen", "")), st["nota"]),
                Paragraph(
                    f"{escapar(it['fuente'])} · "
                    f"{it['fecha'].strftime('%d/%m/%Y')}{eco}{tipo} · "
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

    print("\nDónde se fue el material:")
    print(f"  {perdidos_dup:>5} descartados por duplicado "
          f"(la misma noticia en varios medios)")
    print(f"  {perdidos_vistos:>5} descartados por ya vistos "
          f"(salieron en informes anteriores)")
    print(f"  {perdidos_ruido:>5} descartados por relevancia 1 "
          f"(Claude los consideró ruido)")

    if perdidos_vistos > len(nuevos) * 2 and perdidos_vistos > 10:
        print("\n  → La mayor parte se filtró por 'ya vistos'. Es el sistema")
        print("    funcionando: ya te llegaron. Para ver el panorama completo")
        print("    de un período, corré:  python xeno_clipping.py --rehacer")

    if contar(crudos, "prensa") < 60:
        print("\n  → Llegó poca prensa en bruto. Mirá el listado de búsquedas")
        print("    más arriba: si muchas dicen '<-- sin resultados', Google")
        print("    está devolviendo feeds vacíos. Esperá unos minutos y")
        print("    volvé a correrlo.")

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
    ap.add_argument("--rehacer", action="store_true",
                    help="ignora la base de vistos y NO la actualiza; "
                         "sirve para ver el panorama completo de un período "
                         "sin gastar el estado del sistema")
    args = ap.parse_args()
    VENTANA_DIAS = args.dias

    if not API_KEY:
        print("ERROR: falta la clave de la API.")
        print("Configurá la variable de entorno ANTHROPIC_API_KEY.")
        return

    cliente = Anthropic(api_key=API_KEY)
    con = abrir_base()

    modo = " (modo repaso: se ignora la base de vistos)" if args.rehacer else ""
    print(f"\nVentana: últimos {VENTANA_DIAS} días{modo}")

    print("\n[1/6] Recolectando prensa...")
    crudos = recolectar_google_news()
    print(f"      {len(crudos)} resultados en bruto")

    print("\n[2/6] Recolectando literatura científica y preprints...")
    ciencia = recolectar_ciencia()
    print(f"      {len(ciencia)} papers y preprints")

    print("\n[3/6] Recolectando ensayos y patentes...")
    ensayos = recolectar_ensayos()
    patentes = recolectar_patentes()
    print(f"      {len(ensayos)} ensayos · {len(patentes)} patentes")

    crudos += ciencia + ensayos + patentes

    print("\n[4/6] Eliminando duplicados y ya vistos...")
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

    print("\n[5/6] Clasificando y resumiendo...")
    seleccion, menciones = clasificar(nuevos, cliente)
    print(f"      {len(seleccion)} destacados · {len(menciones)} menciones")

    print("\n[6/6] Generando informe...")
    sello = datetime.now().strftime("%Y-%m-%d")
    sufijo = "_repaso" if args.rehacer else ""
    ruta_pdf = os.path.join(
        CARPETA_SALIDA, f"xenotrasplantes_{sello}{sufijo}.pdf")
    generar_pdf(seleccion, menciones, ruta_pdf)

    if not args.rehacer:
        # Marcamos como vistos TODOS los nuevos, incluso el ruido: así
        # tampoco vuelve a aparecer la semana que viene.
        for i in nuevos:
            marcar_visto(con, i)
        con.commit()

    ruta_csv = os.path.join(CARPETA_SALIDA, "archivo_historico.csv")
    total = exportar_archivo_historico(con, ruta_csv)
    con.close()

    resumen_embudo(crudos, unicos, nuevos, seleccion, menciones)

    print(f"\nInforme:  {ruta_pdf}")
    print(f"Archivo:  {ruta_csv}  ({total} ítems acumulados)\n")


if __name__ == "__main__":
    main()
