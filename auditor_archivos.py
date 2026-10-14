# -*- coding: utf-8 -*-
"""
Auditor Automático de WhatsApp  -  v25.1
Vigila Descargas, lee con OCR los documentos bajados desde WhatsApp Web (SIAC / INE)
y los acomoda en carpetas  FOLIO-NOMBRE APELLIDO-CAPTURISTA.
"""
import base64
import importlib
import importlib.util
import json
import logging
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin, urlparse

VERSION_LOCAL = "25.1"
URL_VERSION_GITHUB = "https://raw.githubusercontent.com/SuperAxxel/Auditor-WhatsApp/refs/heads/main/version.txt"
URL_CODIGO_GITHUB = "https://raw.githubusercontent.com/SuperAxxel/Auditor-WhatsApp/refs/heads/main/auditor_archivos.py"
# Copia de la última versión estable (se sube a mano al repo antes de publicar una versión nueva)
URL_CODIGO_ESTABLE = "https://raw.githubusercontent.com/SuperAxxel/Auditor-WhatsApp/refs/heads/main/estable/auditor_archivos.py"

RUTA_BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CONFIG = os.path.join(RUTA_BASE, "config.json")
ARCHIVO_LOG = os.path.join(RUTA_BASE, "auditor.log")
ARCHIVO_TELEGRAM = os.path.join(RUTA_BASE, "telegram.json")
TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID = ""
URL_RELAY = "https://script.google.com/macros/s/AKfycbw_9cs_Nv_rW19kGK79HBwhV3ukWHT8URFNERvJEBDcCwfeZudsb9QzDmv_BGLKWsuZ/exec"
RUTA_DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")

# Si un RECUADRO no logra leer el nombre: False = se guarda igual como "RECUADRO-SIN NOMBRE hhmmss-..."
#                                         True  = se manda a revisión manual (comportamiento anterior)
RECUADRO_SIN_NOMBRE_A_REVISION = False

# ---------------------------------------------------------------- LOG
class _ConsolaSegura(logging.StreamHandler):
    """Consola que nunca rompe el programa (en algunas PCs con Windows antiguo escribir en stderr falla)."""
    def flush(self):
        try:
            super().flush()
        except Exception:
            pass

    def handleError(self, record):
        pass

def configurar_log():
    logger = logging.getLogger("auditor")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(ARCHIVO_LOG, maxBytes=500_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stderr is not None:
        sh = _ConsolaSegura()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger

log = configurar_log()
sys.excepthook = lambda t, v, tb: log.error("Excepción no controlada", exc_info=(t, v, tb))

# ---------------------------------------------------------------- 1. AUTO-INSTALADOR
DEPENDENCIAS = {  # módulo importable -> paquete de pip
    "cv2": "opencv-python",
    "numpy": "numpy",
    "pytesseract": "pytesseract",
    "watchdog": "watchdog",
    "plyer": "plyer",
    "requests": "requests",
}

def instalar_dependencias():
    faltan = [pip for mod, pip in DEPENDENCIAS.items() if importlib.util.find_spec(mod) is None]
    if not faltan:
        return
    log.info("Instalando dependencias: %s", ", ".join(faltan))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *faltan])
    importlib.invalidate_caches()

instalar_dependencias()

import cv2
import numpy as np
import pytesseract
import requests
import tkinter as tk
import winreg
from plyer import notification
from tkinter import filedialog, messagebox, ttk
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------- 2. TESSERACT
def localizar_tesseract():
    candidatos = [
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
    ]
    for c in candidatos:
        if c and os.path.isfile(c):
            return c
    return None

RUTA_TESSERACT = localizar_tesseract()
if RUTA_TESSERACT:
    pytesseract.pytesseract.tesseract_cmd = RUTA_TESSERACT
    _tessdata = os.path.join(os.path.dirname(RUTA_TESSERACT), "tessdata")
    if os.path.isdir(_tessdata):
        os.environ["TESSDATA_PREFIX"] = _tessdata
os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # 1 hilo por OCR: rinde más al leer varias imágenes en paralelo

def tesseract_listo():
    if not RUTA_TESSERACT:
        return ("No se encontró Tesseract OCR.\nInstálalo (UB-Mannheim) con el idioma Spanish "
                "y vuelve a abrir el programa.")
    if not os.path.isfile(os.path.join(os.environ.get("TESSDATA_PREFIX", ""), "spa.traineddata")):
        return "A Tesseract le falta el idioma español (spa.traineddata)."
    return None

VERSION_TESSERACT = "?"

def registrar_entorno():
    """Deja en el log (y en los reportes) las versiones que usa esta PC: ayuda a diagnosticar equipos antiguos."""
    global VERSION_TESSERACT
    try:
        VERSION_TESSERACT = str(pytesseract.get_tesseract_version())
    except Exception:
        VERSION_TESSERACT = "no disponible"
    log.info("Auditor v%s · Python %s · OpenCV %s · Tesseract %s · %s", VERSION_LOCAL, platform.python_version(),
             cv2.__version__, VERSION_TESSERACT, platform.platform())

# ---------------------------------------------------------------- 3. CONSTANTES Y UTILIDADES
EXT_IMG = (".jpg", ".jpeg", ".png", ".webp")
EXT_TEMPORALES = (".tmp", ".crdownload", ".part", ".download")
ESPERADOS = {"RECUADRO": 3, "DESCARGAR": 4, "WINBACK": 5}          # imágenes + 1 audio
MODO_POR_CANTIDAD = {v: k for k, v in ESPERADOS.items()}           # para nombres antiguos (v24)
ESPERA_LOTE_LEGADO = 4.0
ESPERA_LOTE_INCOMPLETO = 12.0

PREFIJOS = {"DE", "DEL", "LA", "LAS", "LOS", "MAC", "MC", "SAN", "Y", "VAN", "VON"}
ETIQUETAS = {"NOMBRE", "NOMBRES", "CLIENTE"}
PALABRAS_FIN_INE = {"DOMICILIO", "CALLE", "C", "AV", "AVENIDA", "EDAD", "SEXO", "CLAVE", "AÑO", "COL", "COLONIA"}
PALABRAS_INE = ("ELECTORAL", "INSTITUTO", "NACIONAL", "CREDENCIAL", "VOTAR")

# Tolera lecturas sucias del OCR: FOLÍO, FOLLO, F0LIO, "SIAC::" y números pegados
RE_FOLIO = re.compile(r"F[O0]L[ILÍ1|]{1,2}[O0]\s*(?:SIAC|STAC|S1AC|SIA[CG])?\s*[:;.\-]*\s*(\d{5,})")
# Algunas versiones de Tesseract/spa leen la letra O de esta tipografía como «º» (FºLIº, CºNTACTº, DATºS)
_TABLA_OCR = str.maketrans({"º": "O", "°": "O", "˚": "O"})
RE_PREFIJO_NOMBRE = re.compile(r"^(?:NOMBRES?|CLIENTE|TITULAR)\b\s*")
RE_LOTE_NUEVO = re.compile(r"^WP_(RECUADRO|DESCARGAR|WINBACK)_(\d+)_", re.I)
RE_LOTE_IMG_LEGADO = re.compile(r"^WP_IMG_(\d+)_", re.I)
RE_LOTE_AUDIO_LEGADO = re.compile(r"^WP_AUDIO_(\d+)", re.I)

DIAS = ["LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO"]
MESES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
         "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]

MOTIVOS = {
    "sin_folio": "No se encontró el folio SIAC",
    "sin_nombre": "No se pudo leer el nombre",
    "lote_incompleto": "Llegaron archivos incompletos",
    "error": "Error inesperado al procesar",
}

# Agrega la versión nueva ARRIBA de la lista en cada release (la ventana se abre sola al actualizar)
NOVEDADES = [
    ("25.1", "Octubre 2026", [
        "Corregido: en algunas computadoras con Windows antiguo el programa se cerraba al enviar un reporte de error.",
        "Corregido: en computadoras donde la lectura confundía la letra O con «º» (FºLIº SIAC) no se encontraba el folio "
        "y todo se iba a revisión manual. Ahora se lee bien.",
        "Si aun así no se encuentra el folio SIAC, se reintenta la lectura con otros modos antes de mandar el lote a revisión manual.",
        "Los errores de la ventana ya no cierran el programa: aparece un aviso y el detalle queda en el registro.",
        "El registro anota qué se pudo leer de cada imagen (sin nombres de clientes) y las versiones de Python y Tesseract, "
        "para que soporte diagnostique más rápido.",
    ]),
    ("25.0", "Octubre 2026", [
        "Los RECUADROS ya se etiquetan correctamente. Si no se alcanza a leer el nombre, se guardan como "
        "«RECUADRO-SIN NOMBRE» en lugar de irse a revisión manual.",
        "Lectura de la INE más precisa: varios intentos de lectura y corrección con nombres como David, Javier u Octavio.",
        "Procesamiento más rápido: las imágenes de cada cliente se leen al mismo tiempo.",
        "Si un lote llega incompleto, se manda a revisión manual en su propia subcarpeta, con el motivo en el nombre.",
        "Recuerda darle Play al audio de cada cliente antes de descargar desde WhatsApp Web.",
        "Nuevo botón «Reportar error»: envía tu mensaje, una imagen opcional y el registro del programa directo a soporte.",
        "Nuevo botón «Novedades» para volver a ver esta ventana cuando quieras.",
        "Actualizaciones más seguras: se valida el archivo antes de instalarlo y se conserva un respaldo.",
        "El programa ya no se abre dos veces por accidente y avisa si falta Tesseract OCR.",
    ]),
]

def fecha_carpeta(d=None):
    """Igual que '%A %d %B %Y' en español, pero sin depender del locale de Windows."""
    d = d or datetime.now()
    return f"{DIAS[d.weekday()]} {d.day:02d} {MESES[d.month - 1]} {d.year}"

def limpiar_ruta(texto):
    return re.sub(r'[<>:"/\\|?*]', "", texto).strip()

def version_a_tupla(txt):
    return tuple(int(x) for x in re.findall(r"\d+", txt))

def avisar(titulo, mensaje, timeout=3):
    try:
        notification.notify(title=titulo, message=mensaje, app_name="Auditor Automático", timeout=timeout)
    except Exception as e:
        log.warning("No se pudo mostrar la notificación: %s", e)

def mover_seguro(origen, carpeta_destino):
    os.makedirs(carpeta_destino, exist_ok=True)
    base = os.path.basename(origen)
    destino = os.path.join(carpeta_destino, base)
    n = 1
    while os.path.exists(destino):
        raiz, ext = os.path.splitext(base)
        destino = os.path.join(carpeta_destino, f"{raiz}_{n}{ext}")
        n += 1
    for _ in range(5):
        try:
            shutil.move(origen, destino)
            return destino
        except PermissionError:
            time.sleep(0.5)
    raise PermissionError(f"No se pudo mover {origen}")

def clasificar_archivo(nombre):
    """Devuelve (id_lote, modo|None) o None si no es un archivo del auditor."""
    m = RE_LOTE_NUEVO.match(nombre)
    if m:
        return m.group(2), m.group(1).upper()
    m = RE_LOTE_IMG_LEGADO.match(nombre) or RE_LOTE_AUDIO_LEGADO.match(nombre)
    if m:
        return m.group(1), None
    return None

# ---------------------------------------------------------------- 4. LECTURA DE NOMBRES
def preparar_lineas(texto):
    lineas = (re.sub(r"\s+", " ", re.sub(r"[^A-ZÁÉÍÓÚÜÑ0-9 ]", " ", l)).strip() for l in texto.upper().split("\n"))
    return [l for l in lineas if len(l) > 2]

def agrupar_nombres_compuestos(texto):
    """'MARIA DE LA CRUZ' -> ['MARIA', 'DE_LA_CRUZ'] """
    palabras = re.sub(r"[^A-ZÁÉÍÓÚÜÑ0-9 ]", " ", texto.upper()).split()
    resultado, pegamento = [], ""
    for p in palabras:
        if p in PREFIJOS:
            pegamento += p + "_"
        else:
            resultado.append(pegamento + p)
            pegamento = ""
    if pegamento:
        resultado.append(pegamento[:-1])
    return resultado

def nombre_valido(token):
    return bool(re.fullmatch(r"[A-ZÁÉÍÓÚÜÑ_]{2,}", token)) and token not in ETIQUETAS

def extraer_nombre_siac(lineas):
    """Busca 'DATOS ... TITULAR' (negocio) y, si no, 'DATOS ... CONTACTO' (persona)."""
    for clave in ("TITULAR", "CONTACTO"):
        for i, linea in enumerate(lineas):
            if "DATOS" not in linea or clave not in linea:
                continue
            j = i + 1
            while j < len(lineas) and lineas[j] in ETIQUETAS:
                j += 1
            if j >= len(lineas):
                continue
            objetivo = RE_PREFIJO_NOMBRE.sub("", lineas[j]).strip()
            tokens = agrupar_nombres_compuestos(objetivo)
            if not tokens or tokens[0] in ETIQUETAS:
                continue
            if len(tokens) >= 4:
                ap = tokens[-2]
            elif len(tokens) in (2, 3):
                ap = tokens[1]
            else:
                ap = ""
            return tokens[0].replace("_", " "), ap.replace("_", " ")
    return None

def _es_etiqueta_nombre(linea):
    return "NOMBRE" in linea or "N0MBRE" in linea or bool({"NOM", "MBRE"} & set(linea.split()))

def extraer_nombre_ine(lineas):
    """INE: tras 'NOMBRE' vienen apellido paterno, materno y nombre(s)."""
    for i, linea in enumerate(lineas):
        if not _es_etiqueta_nombre(linea):
            continue
        ape1 = lineas[i + 1] if len(lineas) > i + 1 else ""
        ape2 = lineas[i + 2] if len(lineas) > i + 2 else ""
        nom = lineas[i + 3] if len(lineas) > i + 3 else ""
        # Comparamos por palabra completa (antes 'AV' coincidía dentro de DAVID, JAVIER, OCTAVIO...)
        es_domicilio = any(t in PALABRAS_FIN_INE for t in nom.split())
        t_ape = agrupar_nombres_compuestos(ape1)
        t_nom = agrupar_nombres_compuestos(ape2 if (es_domicilio or not nom) else nom)
        if t_nom and nombre_valido(t_nom[0]):
            apellido = t_ape[0].replace("_", " ") if t_ape and nombre_valido(t_ape[0]) else ""
            return t_nom[0].replace("_", " "), apellido
    return None

# ---------------------------------------------------------------- 5. PROCESADOR (OCR)
class ProcesadorDocumentos:
    def __init__(self, publicar_estado):
        self.publicar_estado = publicar_estado
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ocr")

    @staticmethod
    def ocr(img, config=""):
        try:
            texto = pytesseract.image_to_string(img, lang="spa", config=config, timeout=40).upper()
        except Exception as e:
            log.warning("OCR falló: %s", e)
            return ""
        return texto.translate(_TABLA_OCR)

    def leer_imagen(self, ruta):
        try:
            gris = cv2.imdecode(np.fromfile(ruta, np.uint8), cv2.IMREAD_GRAYSCALE)
        except Exception as e:
            log.warning("No se pudo abrir %s: %s", os.path.basename(ruta), e)
            return None
        if gris is None:
            log.warning("Archivo corrupto ignorado: %s", os.path.basename(ruta))
            return None
        if gris.shape[1] < 1600:  # ampliar compensa la compresión de WhatsApp
            gris = cv2.resize(gris, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
        return {"ruta": ruta, "gris": gris, "texto": self.ocr(gris)}

    @staticmethod
    def variantes_ine(gris):
        """Preprocesados para matar hologramas; se generan solo si hacen falta."""
        yield "adaptativo", cv2.adaptiveThreshold(
            gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11), "--psm 3"
        yield "clahe", cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gris), "--psm 6"
        borroso = cv2.GaussianBlur(gris, (3, 3), 0)
        yield "otsu", cv2.threshold(borroso, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1], "--psm 6"

    @staticmethod
    def parece_ine(texto):
        if any(p in texto for p in PALABRAS_INE):
            return True
        return "FOLIO" not in texto and "DATOS" not in texto  # ni SIAC ni texto legible: probamos como INE

    def nombre_desde_ine(self, lec, debug):
        for etiqueta, img, cfg in self.variantes_ine(lec["gris"]):
            texto = self.ocr(img, cfg)
            debug.append(f"===== {os.path.basename(lec['ruta'])} ({etiqueta} {cfg}) =====\n{texto}")
            res = extraer_nombre_ine(preparar_lineas(texto))
            if res:
                log.info("Nombre INE leído con variante '%s'", etiqueta)
                return res
        return extraer_nombre_ine(preparar_lineas(lec["texto"]))  # último recurso: lectura natural

    def reintentar_siac(self, lec, debug):
        """Segunda oportunidad para el folio con otros modos de lectura (útil con Tesseract antiguo)."""
        for etiqueta, cfg in (("psm6", "--psm 6"), ("psm11", "--psm 11")):
            texto = self.ocr(lec["gris"], cfg)
            debug.append(f"===== {os.path.basename(lec['ruta'])} (reintento SIAC {etiqueta}) =====\n{texto}")
            m = RE_FOLIO.search(texto)
            if m:
                log.info("Folio SIAC encontrado con reintento '%s'", etiqueta)
                return m.group(1), extraer_nombre_siac(preparar_lineas(texto))
        return None, None

    def procesar_lote(self, archivos, modo, clave, capturista):
        t0 = time.time()
        es_recuadro = modo == "RECUADRO"
        imagenes = sorted(f for f in archivos if f.lower().endswith(EXT_IMG))
        lecturas = [l for l in self.pool.map(self.leer_imagen, imagenes) if l]
        if len(lecturas) < len(imagenes):
            log.warning("Solo se pudieron leer %d de %d imágenes.", len(lecturas), len(imagenes))

        debug, folio, nombre, apellido, origen = [], None, None, None, None
        diagnostico = None
        for lec in lecturas:
            debug.append(f"===== {os.path.basename(lec['ruta'])} (lectura natural) =====\n{lec['texto']}")
            alto, ancho = lec["gris"].shape[:2]
            log.info("Imagen %s: %dx%d, %d caracteres · FOLIO:%s DATOS:%s INE:%s", os.path.basename(lec["ruta"]),
                     ancho, alto, len(lec["texto"].strip()), "sí" if "FOLIO" in lec["texto"] else "no",
                     "sí" if "DATOS" in lec["texto"] else "no",
                     "sí" if any(p in lec["texto"] for p in PALABRAS_INE) else "no")
            if not es_recuadro and not folio:
                m = RE_FOLIO.search(lec["texto"])
                if m:
                    folio = m.group(1)
                    log.info("Folio SIAC encontrado: %s", folio)
            if origen != "SIAC":
                res = extraer_nombre_siac(preparar_lineas(lec["texto"]))
                if res:
                    nombre, apellido = res
                    origen = "SIAC"
                    log.info("Nombre SIAC procesado")

        if not nombre:
            for lec in lecturas:
                if not self.parece_ine(lec["texto"]):
                    continue
                res = self.nombre_desde_ine(lec, debug)
                if res:
                    nombre, apellido = res
                    origen = "INE"
                    break

        if not es_recuadro and not folio:
            for lec in lecturas:
                if sum(p in lec["texto"] for p in PALABRAS_INE) >= 2:
                    continue  # es una INE: no trae folio SIAC
                folio, res = self.reintentar_siac(lec, debug)
                if res and origen != "SIAC":
                    nombre, apellido = res
                    origen = "SIAC"
                if folio:
                    break

        if es_recuadro:
            folio = "RECUADRO"
        elif not folio:
            log.warning("Ningún folio SIAC legible en %d imagen(es), ni con reintentos.", len(lecturas))
            return self.a_revision(archivos, modo, "sin_folio", debug)

        if not nombre:
            if es_recuadro and not RECUADRO_SIN_NOMBRE_A_REVISION:
                nombre, apellido = f"SIN NOMBRE {datetime.now():%H%M%S}", ""
                log.warning("RECUADRO sin nombre legible; se guarda con etiqueta provisional.")
                diagnostico = list(debug)
                avisar("Recuadro sin nombre", "Se guardó como RECUADRO; revisa el nombre a mano.", 5)
            else:
                return self.a_revision(archivos, modo, "sin_nombre", debug)

        persona = " ".join(p for p in (nombre, apellido) if p)
        carpeta = limpiar_ruta(f"{folio}-{persona}-{capturista}").upper().strip("-. ")
        ruta_final = os.path.join(RUTA_BASE, limpiar_ruta(clave), fecha_carpeta(), carpeta)
        for f in archivos:
            if os.path.exists(f):
                mover_seguro(f, ruta_final)

        if diagnostico:
            try:
                with open(os.path.join(ruta_final, "_diagnostico_ocr.txt"), "w", encoding="utf-8") as fh:
                    fh.write("\n\n".join(diagnostico))
            except Exception as e:
                log.warning("No se pudo guardar el diagnóstico: %s", e)
        log.info("Lote %s procesado, folio %s (%.1fs)", modo, folio, time.time() - t0)
        self.publicar_estado(f"✔ {carpeta}")
        avisar("Éxito", f"Carpeta creada:\n{carpeta}", 3)

    def a_revision(self, archivos, modo, motivo, debug=()):
        ahora = datetime.now()
        carpeta = os.path.join(RUTA_BASE, f"REVISION_MANUAL_{ahora:%Y-%m-%d}",
                               f"{ahora:%H%M%S}-{modo or 'LOTE'}-{motivo}")
        for f in archivos:
            if os.path.exists(f):
                try:
                    mover_seguro(f, carpeta)
                except Exception as e:
                    log.error("No se pudo mover %s a revisión: %s", f, e)
        if debug:
            try:
                with open(os.path.join(carpeta, "_diagnostico_ocr.txt"), "w", encoding="utf-8") as fh:
                    fh.write("\n\n".join(debug))
            except Exception as e:
                log.warning("No se pudo guardar el diagnóstico: %s", e)
        log.warning("Lote %s a revisión manual (%s)", modo, motivo)
        self.publicar_estado(f"⚠ Revisión manual: {MOTIVOS.get(motivo, motivo)}")
        avisar("Atención", f"{MOTIVOS.get(motivo, motivo)}. Archivos movidos a revisión manual.", 5)

# ---------------------------------------------------------------- 6. LOTES + VIGILANTE
class GestorLotes:
    """Agrupa los archivos por lote (mismo timestamp) y procesa un lote a la vez."""

    def __init__(self, procesador, obtener_datos):
        self.procesador = procesador
        self.obtener_datos = obtener_datos
        self.lotes = {}
        self.lock = threading.Lock()
        self.activo = True

    def iniciar(self):
        threading.Thread(target=self._bucle, daemon=True, name="gestor-lotes").start()

    def detener(self):
        self.activo = False

    def registrar(self, ruta):
        nombre = os.path.basename(ruta)
        if not nombre.startswith("WP_") or nombre.lower().endswith(EXT_TEMPORALES):
            return
        info = clasificar_archivo(nombre)
        if not info:
            return
        lote_id, modo = info
        ahora = time.time()
        with self.lock:
            lote = self.lotes.setdefault(lote_id, {"archivos": set(), "modo": modo, "ultimo": ahora})
            if ruta not in lote["archivos"]:
                log.info("Capturado: %s (%d en el lote)", nombre, len(lote["archivos"]) + 1)
            lote["archivos"].add(ruta)
            lote["ultimo"] = ahora
            lote["modo"] = lote["modo"] or modo

    def _lotes_listos(self):
        ahora, listos = time.time(), []
        with self.lock:
            for lote_id, lote in list(self.lotes.items()):
                reposo = ahora - lote["ultimo"]
                esperado = ESPERADOS.get(lote["modo"])
                completo = esperado is not None and len(lote["archivos"]) >= esperado and reposo >= 1.0
                legado = esperado is None and reposo >= ESPERA_LOTE_LEGADO
                if completo or legado or reposo >= ESPERA_LOTE_INCOMPLETO:
                    listos.append(self.lotes.pop(lote_id))
        return listos

    def _bucle(self):
        while self.activo:
            time.sleep(0.5)
            for lote in self._lotes_listos():
                try:
                    self._despachar(lote)
                except Exception:
                    log.exception("Fallo al despachar lote")

    @staticmethod
    def _esperar_estables(archivos, limite=8.0):
        fin, previos = time.time() + limite, {}
        while time.time() < fin:
            tamanos = {}
            for f in archivos:
                try:
                    tamanos[f] = os.path.getsize(f)
                except OSError:
                    tamanos[f] = -1
            if tamanos == previos and all(v > 0 for v in tamanos.values()):
                return
            previos = tamanos
            time.sleep(0.4)

    def _despachar(self, lote):
        archivos = sorted(f for f in lote["archivos"] if os.path.exists(f))
        if not archivos:
            return
        modo = lote["modo"] or MODO_POR_CANTIDAD.get(len(archivos))
        if modo is None:
            log.info("Lote ignorado: %d archivos (se esperan 3, 4 o 5).", len(archivos))
            return
        if len(archivos) < ESPERADOS[modo]:
            self.procesador.a_revision(archivos, modo, "lote_incompleto")
            return
        self._esperar_estables(archivos)
        clave, capturista = self.obtener_datos()
        if not clave or not capturista:
            avisar("Atención", "Falta la clave o el capturista en la ventana del Auditor.", 5)
        avisar("Procesando", f"{modo}: {len(archivos)} archivos detectados. Iniciando lectura...", 2)
        try:
            self.procesador.procesar_lote(archivos, modo, clave or "SIN_CLAVE", capturista or "SIN_CAPTURISTA")
        except Exception:
            log.exception("Error crítico procesando lote")
            self.procesador.a_revision(archivos, modo, "error")

class VigilanteDescargas(FileSystemEventHandler):
    def __init__(self, gestor):
        self.gestor = gestor

    def on_created(self, event):
        if not event.is_directory:
            self.gestor.registrar(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.gestor.registrar(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.gestor.registrar(event.src_path)

# ---------------------------------------------------------------- 6b. REPORTES A TELEGRAM
def cargar_telegram():
    """Token y chat: variables de entorno > telegram.json (junto al script) > constantes."""
    datos = {}
    try:
        with open(ARCHIVO_TELEGRAM, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("telegram.json ilegible: %s", e)
    token = os.environ.get("AUDITOR_TG_TOKEN") or datos.get("token") or TELEGRAM_TOKEN
    chat = os.environ.get("AUDITOR_TG_CHAT") or datos.get("chat_id") or TELEGRAM_CHAT_ID
    return str(token).strip(), str(chat).strip()

def cargar_relay():
    """URL del relay: variable de entorno > telegram.json (relay_url) > constante URL_RELAY."""
    datos = {}
    try:
        with open(ARCHIVO_TELEGRAM, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        pass
    return str(os.environ.get("AUDITOR_RELAY_URL") or datos.get("relay_url") or URL_RELAY).strip()

def _sin_token(texto):
    """Los errores de requests incluyen la URL (y con ella el token): nunca mostrarlos ni guardarlos tal cual."""
    token = cargar_telegram()[0]
    return str(texto).replace(token, "***") if token else str(texto)

def leer_log_reciente(max_bytes=150_000):
    for h in log.handlers:
        if isinstance(h, RotatingFileHandler):
            try:
                h.flush()
            except Exception:
                pass
    try:
        with open(ARCHIVO_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            tam = f.tell()
            f.seek(max(0, tam - max_bytes))
            datos = f.read()
        if tam > max_bytes:
            datos = datos.split(b"\n", 1)[-1]  # descarta la línea cortada
        return datos or None
    except OSError:
        return None

def _validar_telegram(resp):
    try:
        j = resp.json()
    except ValueError:
        raise RuntimeError(f"Respuesta inesperada de Telegram (HTTP {resp.status_code}).") from None
    if not j.get("ok"):
        raise RuntimeError(f"Telegram rechazó el envío: {j.get('description', resp.status_code)}")

def _preparar_imagen_relay(ruta):
    """Devuelve (nombre, mime, bytes). Reduce imágenes pesadas o de formato raro para que el envío sea ligero."""
    with open(ruta, "rb") as f:
        datos = f.read()
    nombre = os.path.basename(ruta)
    if len(datos) > 3_000_000 or not nombre.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        img = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            escala = min(1.0, 1600 / max(img.shape[:2]))
            if escala < 1.0:
                img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                datos, nombre = buf.tobytes(), os.path.splitext(nombre)[0] + ".jpg"
    if len(datos) > 8_000_000:
        raise RuntimeError("la imagen pesa demasiado")
    bajo = nombre.lower()
    mime = "image/png" if bajo.endswith(".png") else "image/webp" if bajo.endswith(".webp") else "image/jpeg"
    return nombre, mime, datos

HOSTS_ECHO = ("googleusercontent.com",)  # Apps Script contesta el POST con una redirección a este dominio
AVISO_SIN_CONFIRMAR = "No se pudo confirmar la recepción del reporte"

def _resumen_respuesta(r):
    """Resumen corto (sin secretos) de una respuesta rara, para dejarlo en auditor.log."""
    cuerpo = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text or "")).strip()[:200]
    return f"HTTP {r.status_code} · {r.headers.get('Content-Type', '?')} · {urlparse(r.url).netloc} · «{cuerpo}»"

def _pista_relay(r):
    t = (r.text or "").lower()
    if "accounts.google.com" in r.url or "sign in" in t or "iniciar sesión" in t:
        return "El relay pide iniciar sesión: publícalo con acceso «Cualquier persona»."
    if r.status_code in (403, 407) or "blocked" in t or "bloquead" in t or "firewall" in t:
        return "La red parece estar bloqueando la respuesta de Google."
    return "Respuesta inesperada del servidor de reportes."

def _enviar_por_relay(url, texto, log_bytes, nombre_log, ruta_imagen):
    """Envía el reporte al relay de Apps Script (que es quien tiene el token). Devuelve avisos."""
    avisos = []
    payload = {"texto": texto[:4000], "version": VERSION_LOCAL}
    if log_bytes:
        payload["log"] = {"nombre": nombre_log, "b64": base64.b64encode(log_bytes).decode("ascii")}
    if ruta_imagen:
        try:
            nombre, mime, datos = _preparar_imagen_relay(ruta_imagen)
            payload["imagen"] = {"nombre": nombre, "mime": mime, "b64": base64.b64encode(datos).decode("ascii")}
        except (OSError, RuntimeError) as e:
            avisos.append(f"no se pudo adjuntar la imagen ({e})")
    # Apps Script primero ejecuta doPost y luego contesta con una redirección (302) donde está el resultado.
    # Seguimos los dos pasos a mano para saber en cuál falla.
    procesado = False
    try:
        r = requests.post(url, json=payload, timeout=90, allow_redirects=False)
        destino = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and destino:
            destino = urljoin(url, destino)
            procesado = urlparse(destino).netloc.endswith(HOSTS_ECHO)
            r = requests.get(destino, timeout=60)
    except requests.RequestException as e:
        if procesado:  # el servidor ya recibió y procesó el reporte; solo falló la confirmación
            log.warning("Relay: reporte procesado pero falló la confirmación (%s)", type(e).__name__)
            return avisos + [AVISO_SIN_CONFIRMAR]
        raise RuntimeError(f"No se pudo contactar al servidor de reportes ({type(e).__name__}).") from None
    try:
        j = r.json()
    except ValueError:
        log.warning("Relay: respuesta que no es JSON -> %s", _resumen_respuesta(r))
        if procesado:
            return avisos + [AVISO_SIN_CONFIRMAR]
        raise RuntimeError(f"{_pista_relay(r)} (HTTP {r.status_code})") from None
    if not j.get("ok"):
        raise RuntimeError(f"El servidor de reportes rechazó el envío: {j.get('error', 'sin detalle')}")
    avisos.extend(str(a) for a in (j.get("avisos") or []))
    return avisos

def enviar_a_telegram(texto, log_bytes=None, nombre_log="auditor_log.txt", ruta_imagen=None):
    """Envía mensaje + adjuntos. Devuelve una lista de avisos (adjuntos que fallaron).
    Lanza RuntimeError si no se pudo enviar el mensaje principal."""
    relay = cargar_relay()
    if relay:
        return _enviar_por_relay(relay, texto, log_bytes, nombre_log, ruta_imagen)
    token, chat = cargar_telegram()
    if not token or not chat:
        raise RuntimeError("El envío de reportes no está configurado en este equipo (falta telegram.json).")
    base = f"https://api.telegram.org/bot{token}"
    avisos = []

    def llamar(metodo, **kw):
        try:
            r = requests.post(f"{base}/{metodo}", timeout=30, **kw)
        except requests.RequestException as e:
            raise RuntimeError(_sin_token(e)) from None
        _validar_telegram(r)

    llamar("sendMessage", data={"chat_id": chat, "text": texto[:4000]})

    if log_bytes:
        try:
            llamar("sendDocument", data={"chat_id": chat, "caption": "📄 Log más reciente"},
                   files={"document": (nombre_log, log_bytes)})
        except RuntimeError as e:
            avisos.append(f"no se pudo adjuntar el log ({e})")

    if ruta_imagen:
        try:
            with open(ruta_imagen, "rb") as f:
                datos = f.read()
            nombre = os.path.basename(ruta_imagen)
            es_foto = nombre.lower().endswith((".jpg", ".jpeg", ".png")) and len(datos) <= 9_000_000
            enviado = False
            if es_foto:
                try:
                    llamar("sendPhoto", data={"chat_id": chat, "caption": "🖼 Imagen adjunta"},
                           files={"photo": (nombre, datos)})
                    enviado = True
                except RuntimeError:
                    pass  # se reintenta como documento
            if not enviado:
                llamar("sendDocument", data={"chat_id": chat, "caption": "🖼 Imagen adjunta"},
                       files={"document": (nombre, datos)})
        except (OSError, RuntimeError) as e:
            avisos.append(f"no se pudo adjuntar la imagen ({e})")
    return avisos

def guardar_reporte_local(texto, log_bytes=None, ruta_imagen=None):
    """Copia del reporte en disco para cuando no hay internet o el bot no responde."""
    carpeta = os.path.join(RUTA_BASE, "REPORTES_PENDIENTES", f"{datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(carpeta, exist_ok=True)
    with open(os.path.join(carpeta, "reporte.txt"), "w", encoding="utf-8") as f:
        f.write(texto)
    if log_bytes:
        with open(os.path.join(carpeta, "auditor_log.txt"), "wb") as f:
            f.write(log_bytes)
    if ruta_imagen and os.path.isfile(ruta_imagen):
        shutil.copy2(ruta_imagen, carpeta)
    return carpeta

# ---------------------------------------------------------------- 6c. VENTANAS (NOVEDADES / REPORTE)
class VentanaNovedades(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Novedades del Auditor")
        self.geometry("500x460")
        self.transient(master)

        marco = tk.Frame(self)
        marco.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        barra = ttk.Scrollbar(marco)
        texto = tk.Text(marco, wrap="word", yscrollcommand=barra.set, font=("Segoe UI", 10),
                        relief="flat", padx=8, pady=8, bg=self.cget("bg"))
        barra.config(command=texto.yview)
        barra.pack(side="right", fill="y")
        texto.pack(side="left", fill="both", expand=True)

        texto.tag_configure("ver", font=("Segoe UI", 13, "bold"), spacing1=8, spacing3=4)
        texto.tag_configure("fecha", font=("Segoe UI", 9), foreground="gray")
        texto.tag_configure("item", lmargin1=14, lmargin2=30, spacing3=4)
        for version, fecha, items in NOVEDADES:
            texto.insert("end", f"Versión {version}", "ver")
            texto.insert("end", f"   {fecha}\n", "fecha")
            for it in items:
                texto.insert("end", f"•  {it}\n", "item")
            texto.insert("end", "\n")
        texto.config(state="disabled")

        tk.Button(self, text="Entendido", width=14, command=self.destroy).pack(pady=10)

class VentanaReporte(tk.Toplevel):
    COOLDOWN_SEGUNDOS = 30
    MAX_IMAGEN_BYTES = 20_000_000

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Reportar un error")
        self.geometry("470x560")
        self.resizable(False, False)
        self.transient(app)
        self.ruta_imagen = None
        self.cola = queue.Queue()

        self.formulario = f = tk.Frame(self)
        f.pack(fill="both", expand=True, padx=16, pady=10)

        tk.Label(f, text="Tu nombre *").pack(anchor="w")
        self.var_nombre = tk.StringVar(value=app.datos_actuales[1].title())
        tk.Entry(f, textvariable=self.var_nombre).pack(fill="x", pady=(0, 8))

        tk.Label(f, text="Celular (opcional)").pack(anchor="w")
        self.var_celular = tk.StringVar()
        tk.Entry(f, textvariable=self.var_celular).pack(fill="x", pady=(0, 8))

        tk.Label(f, text="¿Qué pasó? Cuéntanos los detalles *").pack(anchor="w")
        self.txt_detalles = tk.Text(f, height=8, wrap="word")
        self.txt_detalles.pack(fill="x", pady=(0, 8))

        fila = tk.Frame(f)
        fila.pack(fill="x", pady=(0, 8))
        tk.Button(fila, text="Adjuntar imagen (opcional)…", command=self.elegir_imagen).pack(side="left")
        self.label_imagen = tk.Label(fila, text="Ninguna", fg="gray")
        self.label_imagen.pack(side="left", padx=8)
        self.boton_quitar = tk.Button(fila, text="Quitar", command=self.quitar_imagen, state="disabled")
        self.boton_quitar.pack(side="right")

        self.var_log = tk.BooleanVar(value=True)
        tk.Checkbutton(f, variable=self.var_log, justify="left",
                       text="Adjuntar el registro (log) más reciente del programa.\n"
                            "No incluye nombres de clientes.").pack(anchor="w", pady=(0, 8))

        self.label_estado = tk.Label(f, text="", fg="gray", wraplength=430)
        self.label_estado.pack(pady=(0, 6))
        botones = tk.Frame(f)
        botones.pack()
        self.boton_enviar = tk.Button(botones, text="Enviar reporte", width=16, command=self.enviar)
        self.boton_enviar.pack(side="left", padx=6)
        tk.Button(botones, text="Cancelar", width=12, command=self.destroy).pack(side="left", padx=6)

    # --- adjuntos
    def elegir_imagen(self):
        ruta = filedialog.askopenfilename(
            parent=self, title="Elige una imagen",
            filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            if os.path.getsize(ruta) > self.MAX_IMAGEN_BYTES:
                messagebox.showwarning("Imagen muy pesada", "La imagen pesa más de 20 MB. Elige una más ligera.", parent=self)
                return
        except OSError:
            return
        self.ruta_imagen = ruta
        self.label_imagen.config(text=os.path.basename(ruta)[:30], fg="black")
        self.boton_quitar.config(state="normal")

    def quitar_imagen(self):
        self.ruta_imagen = None
        self.label_imagen.config(text="Ninguna", fg="gray")
        self.boton_quitar.config(state="disabled")

    # --- envío
    def enviar(self):
        nombre = self.var_nombre.get().strip()
        celular = self.var_celular.get().strip()
        detalles = self.txt_detalles.get("1.0", "end").strip()

        if not nombre:
            messagebox.showwarning("Falta tu nombre", "Escribe tu nombre para que soporte sepa quién reporta.", parent=self)
            return
        if len(detalles) < 10:
            messagebox.showwarning("Faltan detalles", "Cuéntanos con un poco más de detalle qué pasó.", parent=self)
            return
        if celular and len(re.sub(r"\D", "", celular)) < 8:
            messagebox.showwarning("Celular incompleto", "Revisa el número de celular o déjalo vacío.", parent=self)
            return
        if time.time() - getattr(self.app, "ultimo_reporte", 0) < self.COOLDOWN_SEGUNDOS:
            messagebox.showinfo("Reporte reciente", "Ya enviaste un reporte hace un momento. Espera unos segundos.", parent=self)
            return

        clave, capturista = self.app.datos_actuales
        texto = (
            f"🐞 REPORTE DE ERROR — Auditor v{VERSION_LOCAL}\n"
            f"👤 {nombre}\n"
            f"📱 {celular or '—'}\n"
            f"💻 {platform.node()} · {platform.platform()}\n"
            f"🔧 Python {platform.python_version()} · OpenCV {cv2.__version__} · Tesseract {VERSION_TESSERACT}\n"
            f"🗂 Clave: {clave or '—'} · Capturista: {capturista or '—'}\n"
            f"🕒 {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"📍 Último estado: {self.app.label_estado.cget('text')}\n\n"
            f"📝 Detalles:\n{detalles[:3000]}"
        )
        log_bytes = leer_log_reciente() if self.var_log.get() else None
        nombre_seguro = re.sub(r"\W+", "_", nombre)
        nombre_log = f"auditor_log_{nombre_seguro}_{datetime.now():%Y%m%d_%H%M%S}.txt"

        self.boton_enviar.config(state="disabled")
        self.label_estado.config(text="Enviando reporte...", fg="blue")
        threading.Thread(target=self._trabajo, args=(texto, log_bytes, nombre_log, self.ruta_imagen),
                         daemon=True).start()
        self.after(200, self._poll)

    def _trabajo(self, texto, log_bytes, nombre_log, ruta_imagen):
        try:
            avisos = enviar_a_telegram(texto, log_bytes, nombre_log, ruta_imagen)
            self.cola.put(("ok", avisos, None))
        except Exception as e:
            motivo = _sin_token(e)
            log.warning("No se pudo enviar el reporte: %s", motivo)
            carpeta = None
            try:
                carpeta = guardar_reporte_local(texto, log_bytes, ruta_imagen)
            except Exception as e2:
                log.warning("No se pudo guardar el reporte local: %s", e2)
            self.cola.put(("error", motivo, carpeta))

    def _poll(self):
        if not self.winfo_exists():  # la cerraron mientras se enviaba
            return
        try:
            tipo, a, b = self.cola.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        self._mostrar_final(tipo, a, b)

    # --- pantalla final: seguir con la actual o volver a la estable
    def _mostrar_final(self, tipo, a, b):
        for w in self.formulario.winfo_children():
            w.destroy()
        f = self.formulario
        if tipo == "ok":
            self.app.ultimo_reporte = time.time()
            titulo, detalle = "✅ Reporte enviado", "Gracias, soporte ya lo recibió."
            if any(x.startswith(AVISO_SIN_CONFIRMAR) for x in a):
                detalle = ("El reporte se envió, pero el servidor no confirmó la recepción.\n"
                           "Si soporte no se comunica contigo, vuelve a intentarlo o avísales directamente.")
                a = [x for x in a if not x.startswith(AVISO_SIN_CONFIRMAR)]
            if a:
                detalle += "\n\nNota: " + "; ".join(a)
        else:
            titulo = "⚠️ No se pudo enviar el reporte"
            detalle = f"Motivo: {a}"
            if b:
                detalle += f"\n\nSe guardó una copia en:\n{b}\nPásala a soporte en cuanto puedas."

        tk.Label(f, text=titulo, font=("Segoe UI", 13, "bold")).pack(pady=(10, 4))
        tk.Label(f, text=detalle, wraplength=420, justify="left").pack(pady=4)
        tk.Label(f, text="¿Cómo quieres seguir?", font=("Segoe UI", 10, "bold")).pack(pady=(18, 6))
        tk.Button(f, text="Descargar la versión estable anterior", command=self.usar_estable).pack(fill="x", pady=3)
        tk.Label(f, text="Se instala la versión estable anterior y el programa se reinicia.",
                 fg="gray", wraplength=420).pack()
        tk.Button(f, text="Continuar con la versión actual", command=self.destroy).pack(fill="x", pady=(12, 3))

    def usar_estable(self):
        if messagebox.askyesno("Versión estable",
                               "El programa se cerrará y se abrirá de nuevo con la versión estable anterior.\n\n¿Continuar?",
                               parent=self):
            self.app.iniciar_instalacion(URL_CODIGO_ESTABLE, rollback=True)
            self.destroy()

# ---------------------------------------------------------------- 7. INTERFAZ
def ejecutable_sin_consola():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Auditor Automático v{VERSION_LOCAL}")
        self.geometry("380x390")
        self.resizable(False, False)
        self.cfg = self.cargar_config()
        self.cola_ui = queue.Queue()
        self.datos_actuales = ("", "")

        self.label_alerta = tk.Label(self, text="", fg="red", font=("Arial", 10, "bold"), wraplength=330)
        self.label_alerta.pack(pady=(30, 0))

        self.var_clave = tk.StringVar(value=self.cfg.get("ultima_clave", ""))
        self.var_capturista = tk.StringVar(value=self.cfg.get("ultimo_capturista", ""))

        tk.Label(self, text="Clave de Captura:").pack(pady=(10, 0))
        self.combo_clave = ttk.Combobox(self, textvariable=self.var_clave,
                                        values=self.cfg.get("claves", ["CLAVE-01"]))
        self.combo_clave.pack()

        tk.Label(self, text="Nombre del Capturista:").pack(pady=(10, 0))
        self.combo_capturista = ttk.Combobox(self, textvariable=self.var_capturista,
                                             values=self.cfg.get("capturistas", ["JUAN PEREZ"]))
        self.combo_capturista.pack()

        self.var_inicio = tk.BooleanVar(value=self.cfg.get("auto_inicio", False))
        tk.Checkbutton(self, text="Iniciar con Windows", variable=self.var_inicio,
                       command=self.toggle_inicio).pack(pady=10)
        tk.Button(self, text="Guardar y Minimizar", command=self.guardar_y_minimizar).pack(pady=5)

        self.label_estado = tk.Label(self, text="Vigilando Descargas...", fg="gray", wraplength=330)
        self.label_estado.pack(pady=10)

        tk.Button(self, text="Novedades", command=self.mostrar_novedades, relief="flat",
                  fg="#1565c0", cursor="hand2").place(relx=1.0, x=-6, y=6, anchor="ne")
        tk.Button(self, text="🐞 Reportar error", command=self.abrir_reporte, relief="flat",
                  fg="#d32f2f", cursor="hand2").place(relx=1.0, rely=1.0, x=-6, y=-6, anchor="se")

        # Los hilos leen self.datos_actuales (no tocan widgets de Tk)
        self.var_clave.trace_add("write", self._sincronizar_datos)
        self.var_capturista.trace_add("write", self._sincronizar_datos)
        self._sincronizar_datos()

        self.procesador = ProcesadorDocumentos(lambda t: self.cola_ui.put(("estado", t)))
        self.gestor = GestorLotes(self.procesador, lambda: self.datos_actuales)
        self.observer = Observer()
        self.observer.schedule(VigilanteDescargas(self.gestor), RUTA_DESCARGAS, recursive=False)
        self.observer.start()
        self.gestor.iniciar()

        threading.Thread(target=self.revisar_actualizacion, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.cerrar_app)
        self.after(300, self.inicio_ventanas)
        self.after(500, self._poll_ui)

    # --- utilidades de UI
    def _sincronizar_datos(self, *_):
        self.datos_actuales = (self.var_clave.get().strip().upper(),
                               self.var_capturista.get().strip().upper())

    def _poll_ui(self):
        try:
            while True:
                tipo, valor = self.cola_ui.get_nowait()
                if tipo == "estado":
                    self.label_estado.config(text=valor, fg="black")
                elif tipo == "actualizacion":
                    self.preguntar_actualizacion(valor)
                elif tipo == "error_update":
                    self.label_alerta.config(text="")
                    messagebox.showerror("Error de Actualización",
                                         f"No se pudo actualizar:\n{valor}\nPor favor avisa a soporte técnico.")
                elif tipo == "reiniciar":
                    self.cerrar_app()
                    return
        except queue.Empty:
            pass
        self.after(500, self._poll_ui)

    def verificar_tesseract(self):
        problema = tesseract_listo()
        if problema:
            messagebox.showwarning("Falta Tesseract OCR", problema)

    def inicio_ventanas(self):
        self.verificar_tesseract()
        if self.cfg.get("version_novedades_vista") != VERSION_LOCAL:  # primera vez o recién actualizado
            self.mostrar_novedades()

    def _abrir_unica(self, atributo, clase):
        v = getattr(self, atributo, None)
        if v is not None and v.winfo_exists():
            v.lift()
            v.focus_force()
            return
        setattr(self, atributo, clase(self))

    def mostrar_novedades(self):
        self._abrir_unica("ventana_novedades", VentanaNovedades)
        if self.cfg.get("version_novedades_vista") != VERSION_LOCAL:
            self.cfg["version_novedades_vista"] = VERSION_LOCAL
            self.guardar_config()

    def abrir_reporte(self):
        self._abrir_unica("ventana_reporte", VentanaReporte)

    def report_callback_exception(self, exc, val, tb):
        """Tk imprime en stderr (que puede fallar en Windows antiguo y cerrar el programa): lo mandamos al log."""
        log.error("Error en la interfaz", exc_info=(exc, val, tb))
        try:
            messagebox.showerror("Error inesperado",
                                 f"Ocurrió un error: {val}\n\nEl detalle quedó en auditor.log. "
                                 "Puedes avisar con el botón «Reportar error».")
        except Exception:
            pass

    # --- actualización
    def revisar_actualizacion(self):
        try:
            r = requests.get(URL_VERSION_GITHUB, timeout=5)
            r.raise_for_status()
            nube = r.text.strip()
            if (re.fullmatch(r"\d+(\.\d+)*", nube)
                    and version_a_tupla(nube) > version_a_tupla(VERSION_LOCAL)
                    and version_a_tupla(nube) > version_a_tupla(self.cfg.get("ignorar_hasta", "0"))):
                self.cola_ui.put(("actualizacion", nube))
        except Exception as e:
            log.info("No se pudo verificar la actualización: %s", e)

    def preguntar_actualizacion(self, version_nube):
        if messagebox.askyesno(
                "¡Actualización Disponible!",
                f"Se ha detectado la versión {version_nube} en el servidor.\n\n"
                f"¿Deseas descargarla e instalarla ahora? El programa se reiniciará."):
            self.iniciar_instalacion(URL_CODIGO_GITHUB, version_nube)

    def iniciar_instalacion(self, url, version_esperada=None, rollback=False):
        """Descarga, valida e instala un .py nuevo (actualización o regreso a la versión estable) y reinicia."""
        if rollback:
            self.cfg["ignorar_hasta"] = VERSION_LOCAL  # no volver a ofrecer la versión que dio problemas
            self.guardar_config()
        self.label_alerta.config(text="Descargando versión estable..." if rollback else "Descargando actualización...",
                                 fg="blue")
        respaldo = os.path.abspath(__file__) + ".bak" if rollback else None
        threading.Thread(target=self._instalar_codigo, args=(url, version_esperada, respaldo), daemon=True).start()

    def _instalar_codigo(self, url, version_esperada, respaldo_local=None):
        try:
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                codigo = r.content
            except Exception as e:
                # Regreso a la estable sin internet / sin copia en GitHub: usar la versión que había antes (.bak)
                if respaldo_local and os.path.isfile(respaldo_local):
                    log.warning("No se pudo descargar la versión estable (%s); se usa la copia local anterior.", e)
                    with open(respaldo_local, "rb") as f:
                        codigo = f.read()
                else:
                    raise
            texto = codigo.decode("utf-8-sig")
            compile(texto, "auditor_nuevo.py", "exec")  # nunca instalar algo que no compila
            m = re.search(r'^VERSION_LOCAL\s*=\s*["\']?([\d.]+)', texto, re.M)
            if not m:
                raise RuntimeError("El archivo descargado no parece ser el Auditor.")
            if version_esperada and version_a_tupla(m.group(1)) < version_a_tupla(version_esperada):
                raise RuntimeError("GitHub todavía no sirve el archivo nuevo (caché). Intenta en unos minutos.")

            archivo_actual = os.path.abspath(__file__)
            archivo_temp = os.path.join(RUTA_BASE, "auditor_temp.py")
            archivo_bat = os.path.join(RUTA_BASE, "actualizador.bat")
            with open(archivo_temp, "wb") as f:
                f.write(codigo)
            shutil.copy2(archivo_actual, archivo_actual + ".bak")

            codigo_bat = (
                "@echo off\n"
                "chcp 65001 > NUL\n"
                "ping 127.0.0.1 -n 3 > NUL\n"
                f'move /Y "{archivo_temp}" "{archivo_actual}" > NUL\n'
                f'cd /d "{RUTA_BASE}"\n'
                f'start "" "{sys.executable}" "{archivo_actual}"\n'
                'del "%~f0"\n'
            )
            with open(archivo_bat, "w", encoding="utf-8") as f:
                f.write(codigo_bat)
            subprocess.Popen(["cmd", "/c", archivo_bat], creationflags=subprocess.CREATE_NO_WINDOW)
            self.cola_ui.put(("reiniciar", None))
        except Exception as e:
            log.exception("Fallo la actualización")
            self.cola_ui.put(("error_update", str(e)))

    # --- configuración
    @staticmethod
    def cargar_config():
        try:
            with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("config.json ilegible, se usa uno nuevo: %s", e)
        return {"claves": [], "capturistas": []}

    def guardar_config(self):
        clave, capturista = self.datos_actuales
        claves = list(self.combo_clave["values"])
        capturistas = list(self.combo_capturista["values"])
        if clave and clave not in claves:
            claves.append(clave)
        if capturista and capturista not in capturistas:
            capturistas.append(capturista)
        self.combo_clave["values"] = claves
        self.combo_capturista["values"] = capturistas

        self.cfg.update({"claves": claves, "capturistas": capturistas,
                         "ultima_clave": clave, "ultimo_capturista": capturista,
                         "auto_inicio": self.var_inicio.get()})
        try:
            tmp = ARCHIVO_CONFIG + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ARCHIVO_CONFIG)
        except Exception as e:
            log.warning("No se pudo guardar config.json: %s", e)

    def toggle_inicio(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_ALL_ACCESS) as reg:
                if self.var_inicio.get():
                    cmd = f'"{ejecutable_sin_consola()}" "{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(reg, "AuditorWhatsApp", 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(reg, "AuditorWhatsApp")
                    except FileNotFoundError:
                        pass
        except Exception as e:
            log.warning("Error en registro: %s", e)

    def guardar_y_minimizar(self):
        self.guardar_config()
        self.iconify()

    def cerrar_app(self):
        self.guardar_config()
        self.gestor.detener()
        self.observer.stop()
        self.observer.join(timeout=2)
        self.destroy()

# ---------------------------------------------------------------- 8. INICIO
def instancia_unica():
    """Evita dos copias vigilando Descargas a la vez (reintenta por si viene de una actualización)."""
    for _ in range(6):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            opcion = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if opcion is not None:
                s.setsockopt(socket.SOL_SOCKET, opcion, 1)
            s.bind(("127.0.0.1", 47653))
            return s
        except OSError:
            s.close()
            time.sleep(1)
    return None

if __name__ == "__main__":
    candado = instancia_unica()
    if candado is None:
        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showinfo("Auditor Automático", "El programa ya está abierto.")
        sys.exit(0)
    registrar_entorno()
    App().mainloop()
