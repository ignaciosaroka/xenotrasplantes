# Clipping de xenotrasplantes — versión 2

Ahora cubre las cinco capas de fuentes que salían de las conversaciones de tu
abuelo, no sólo la prensa.

---

## Las cinco capas

| Capa | De dónde sale | ¿Clave? |
|---|---|---|
| **Prensa** | Google News RSS, 46 búsquedas (español, inglés y por nombre propio) | no |
| **Papers y preprints** | Europe PMC — indexa PubMed + bioRxiv + medRxiv en una sola consulta | no |
| **Ensayos clínicos** | ClinicalTrials.gov, API v2 | no |
| **Patentes** | PatentsView (USPTO) | sí, gratuita |
| **Regulatorio** | búsquedas dirigidas: FDA, EMA, INCUCAI, OMS | no |

Cuatro de las cinco funcionan sin registrarte en nada. Sólo las patentes
piden una clave.

---

## Paso 1 — Librerías (igual que antes)

```
pip install feedparser anthropic reportlab
```

Si ya lo corriste la vez pasada, no hace falta repetirlo.

---

## Paso 2 — Reemplazar el script

Pisá el `xeno_clipping.py` viejo con el nuevo. **No borres `vistos.db`**:
el archivo nuevo lo sigue usando igual, así que no vas a recibir de nuevo lo
que ya te llegó.

---

## Paso 3 — Revisar la configuración

Abrí el archivo y mirá el bloque `CONFIGURACION`. Lo único obligatorio es la
carpeta:

```python
CARPETA_SALIDA = r"C:\Users\nsaro\OneDrive\xenotrasplantes"
```

Lo demás viene con valores razonables. Las perillas útiles:

```python
VENTANA_DIAS = 7              # cuántos días hacia atrás
UMBRAL_RELEVANCIA = 3         # 4 si llega mucho ruido, 2 si se pierde cosas
INCLUIR_CIENCIA = True        # papers y preprints
INCLUIR_ENSAYOS = True        # ClinicalTrials.gov
INCLUIR_MENCIONES_BREVES = True
```

---

## Paso 4 (opcional) — Clave de patentes

Las patentes son la fuente más subutilizada: muestran líneas porcinas y
técnicas de edición años antes de que aparezcan en un paper. Requieren un
trámite de un minuto:

1. Entrá a `https://patentsview.org/apis/keyrequest`
2. Pedí la clave (es gratuita, llega por mail).
3. Guardala como variable de entorno:

```
setx PATENTSVIEW_API_KEY "la-clave-que-te-llego"
```

Si no la configurás, el script imprime `(patentes: sin clave configurada, se
omite)` y sigue con todo lo demás. No se rompe nada.

---

## Paso 5 — Correr

```
python xeno_clipping.py
```

Ahora son seis etapas y tarda un poco más (6–8 minutos), casi todo esperando
a Google News:

```
[1/6] Recolectando prensa...
[2/6] Recolectando literatura científica y preprints...
[3/6] Recolectando ensayos y patentes...
[4/6] Eliminando duplicados y ya vistos...
[5/6] Clasificando y resumiendo...
[6/6] Generando informe...
```

---

## Qué cambió en el informe

**Nueve categorías** en vez de seis: se separaron *Hitos clínicos* de
*Ciencia y preprints*, y se agregaron *Ensayos clínicos* y *Patentes*.

**Sección "Otras menciones"** al final: los ítems que Claude puntúa en 2 ya
no se tiran. Aparecen en una línea, sin resumen, con el link. Así nada queda
invisible y vos decidís si algo merecía más atención — que es justamente la
forma de calibrar el umbral.

**Etiquetas de origen** en cada ítem: `paper`, `preprint`, `registro de
ensayo`, `patente`. Los de prensa no llevan etiqueta.

**Archivo histórico**: además del PDF, el script escribe
`archivo_historico.csv` con todo lo acumulado desde el día uno — fecha,
título, fuente y link. Se abre en Excel. Con los meses se vuelve el registro
del campo, y es la semilla natural de cualquier base de conocimiento que
quieran armar después.

---

## Qué dejé afuera a propósito

**Alertas de tabla de contenidos de revistas** (Xenotransplantation, AJT,
Nature Medicine, NEJM, JAMA). Son redundantes: Europe PMC ya indexa las
cinco, normalmente el mismo día. Sirven para que una persona hojee el índice
de un número, no para un sistema que busca por palabra clave.

**Abstracts de congresos** (American Transplant Congress, ESOT, congreso de
la IXA). Es una fuente genuinamente valiosa — los resultados se presentan
hasta un año antes de publicarse — pero no tiene feed ni API. Hay que
descargar el libro de resúmenes cuando sale. Va al calendario a mano, una o
dos veces al año.

**Zotero, Notion, la base de casos.** Son un producto distinto del clipping:
una base de conocimiento que alguien tiene que mantener. El CSV histórico
cubre buena parte del valor sin trabajo adicional. Si en algún momento
quieren la base completa de casos cerdo-humano (centro, órgano,
supervivencia, causa de falla, esquema inmunosupresor), eso se arma aparte y
conviene hacerlo con los investigadores, no solos.

---

## Si algo falla

| Síntoma | Causa habitual |
|---|---|
| `ModuleNotFoundError` | Falta el `pip install` |
| `ERROR: falta la clave de la API` | Variable de entorno ausente, o no reabriste la terminal |
| `ERROR en Europe PMC` / `en ClinicalTrials.gov` | Servicio caído o sin conexión; el script sigue con las demás fuentes |
| `(patentes: sin clave configurada, se omite)` | Normal si saltaste el Paso 4 |
| Sigue llegando poco | Bajá `UMBRAL_RELEVANCIA` a 2 por una semana y mirá qué aparece en "Otras menciones" |

---

## Diagnóstico: por qué salen pocos ítems

Al terminar, el script ahora imprime una tabla que muestra dónde se angosta
el embudo, fuente por fuente:

```
fuente         crudos   únicos   nuevos  informe
------------------------------------------------
prensa            184       38        6        3
paper               7        7        1        1
preprint            2        2        0        0
ensayo              1        1        0        0
patente             0        0        0        0
------------------------------------------------
TOTAL             194       48        7        4

Dónde se fue el material:
    146 descartados por duplicado
     41 descartados por ya vistos
      3 descartados por relevancia 1
```

Leela así:

- **Caída de "crudos" a "únicos"** → normal y deseable. Un hito lo replican
  cincuenta medios.
- **Caída de "únicos" a "nuevos"** → la base de vistos. Si acá se va casi
  todo, es porque ya te llegó en informes anteriores. **Es el sistema
  funcionando, no fallando.**
- **Caída de "nuevos" a "informe"** → el filtro de relevancia. Si acá se va
  mucho, bajá `UMBRAL_RELEVANCIA`.
- **Una fila entera en cero** → esa fuente no está trayendo nada. Mirá si
  dio error más arriba en la consola.

---

## Dos comandos nuevos

### Ver el panorama completo de un período

```
python xeno_clipping.py --rehacer --dias 30
```

Ignora la base de vistos y arma un informe con **todo** lo del último mes,
como si fuera la primera vez. El PDF sale con el sufijo `_repaso`.

Importante: en este modo **no** se actualiza la base de vistos. Es un
informe de lectura, no consume el estado del sistema, así que podés correrlo
las veces que quieras sin arruinar la secuencia semanal.

Usalo ahora mismo para ver cuánto material hay realmente. Si con 30 días y
sin filtro de vistos siguen saliendo pocos ítems, el problema son las
búsquedas y ahí sí hay que ampliarlas. Si salen cuarenta, entonces las tres
de esta semana eran, simplemente, las tres novedades de la semana.

### Cambiar la ventana

```
python xeno_clipping.py --dias 14
```

Sin `--rehacer`, sí actualiza la base de vistos con normalidad.
