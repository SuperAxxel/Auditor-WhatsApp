import os
import sys
import json
import time
import shutil
import threading
import subprocess
import re
from datetime import datetime
import locale

VERSION_LOCAL = 24.3
URL_VERSION_GITHUB = "https://raw.githubusercontent.com/SuperAxxel/Auditor-WhatsApp/refs/heads/main/version.txt"
URL_CODIGO_GITHUB = "https://raw.githubusercontent.com/SuperAxxel/Auditor-WhatsApp/refs/heads/main/auditor_archivos.py"

# --- 1. AUTO-INSTALADOR DE DEPENDENCIAS ---
def instalar_dependencias():
    paquetes = ['opencv-python', 'pytesseract', 'watchdog', 'plyer', 'requests']
    for paquete in paquetes:
        try:
            __import__(paquete if paquete != 'opencv-python' else 'cv2')
        except ImportError:
            print(f"Instalando {paquete}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", paquete])

instalar_dependencias()

import cv2
import numpy as np
import pytesseract
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from plyer import notification
import tkinter as tk
from tkinter import ttk
import winreg
import requests
from tkinter import messagebox

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
os.environ['TESSDATA_PREFIX'] = r'C:\Program Files\Tesseract-OCR\tessdata'

RUTA_BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CONFIG = os.path.join(RUTA_BASE, 'config.json')
RUTA_DESCARGAS = os.path.join(os.path.expanduser('~'), 'Downloads')

try:
    locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
except:
    pass 

class ProcesadorDocumentos:
    
    # --- FUNCIÓN LINGÜÍSTICA PARA NOMBRES COMPUESTOS ---
    def agrupar_nombres_compuestos(self, texto):
        # Limpiamos basura del OCR
        palabras = texto.replace(',', '').replace('.', '').replace('-', '').split()
        prefijos = {"DE", "DEL", "LA", "LAS", "LOS", "MAC", "MC", "SAN", "Y", "VAN", "VON"}
        
        resultado = []
        compuesto = ""
        
        for p in palabras:
            if p in prefijos:
                compuesto += p + "_" # Le ponemos pegamento
            else:
                resultado.append(compuesto + p)
                compuesto = "" # Reiniciamos el pegamento
                
        if compuesto: # Por si el OCR cortó la lectura a la mitad
            resultado.append(compuesto[:-1])
            
        return resultado

    def procesar_archivos(self, lista_archivos, clave, capturista):
        try:
            nombre_final = "DESC"
            apellido_final = "DESC"
            folio_final = "DESC"
            
            es_recuadro = (len(lista_archivos) == 3)
            if es_recuadro:
                folio_final = "RECUADRO"
                print("Modo RECUADRO detectado automáticamente.")

            for ruta_archivo in lista_archivos:
                # Evitar leer el archivo de audio (.ogg) con el OCR
                if not ruta_archivo.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    continue

                img_array = np.fromfile(ruta_archivo, np.uint8)
                img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                
                if img is None:
                    print(f"Alerta: Archivo corrupto ignorado ({os.path.basename(ruta_archivo)})")
                    continue

                # --- PRE-PROCESADO BASE ---
                gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                # Ampliamos 1.5x (Vital para contrarrestar la compresión de WhatsApp)
                gris_ampliado = cv2.resize(gris, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)

                # --- PASADA 1: LECTURA NATURAL (Ideal para el SIAC) ---
                # Sin whitelist destructiva para que respete acentos y la Ñ
                texto_base = pytesseract.image_to_string(gris_ampliado, lang='spa')
                texto_limpio = texto_base.upper()

                # --- 1. BÚSQUEDA DEL FOLIO SIAC ---
                if not es_recuadro and folio_final == "DESC":
                    # Flexibilidad: Permite que lea "FOLIO SIAC", "FOLIO STAC" o espacios extra
                    match_folio = re.search(r'FOLIO\s*(?:SIAC|STAC|S1AC)?\s*[:\-]?\s*(\d+)', texto_limpio)
                    if match_folio:
                        folio_final = match_folio.group(1)
                        print(f"¡Folio SIAC encontrado!: {folio_final}")

                # --- 2. BÚSQUEDA DEL NOMBRE EN EL SIAC (DOBLE ESTRATEGIA) ---
                nombre_encontrado = False
                
                # A. ESTRATEGIA PARA NEGOCIOS (Prioridad: Buscar "TITULAR")
                if "DATOS" in texto_limpio and "TITULAR" in texto_limpio:
                    lineas = [l.strip() for l in texto_limpio.split('\n') if len(l.strip()) > 2]
                    for i, linea in enumerate(lineas):
                        if "DATOS" in linea and "TITULAR" in linea and len(lineas) > i+1:
                            linea_objetivo = lineas[i+1]
                            
                            # Filtro para saltar etiquetas basura
                            etiquetas = ["NOMBRE", "NOMBRE:", "NOMBRES", "NOMBRES:", "CLIENTE", "CLIENTE:"]
                            saltos = 1
                            while linea_objetivo in etiquetas and (i + saltos + 1) < len(lineas):
                                saltos += 1
                                linea_objetivo = lineas[i+saltos]
                                
                            nc = self.agrupar_nombres_compuestos(linea_objetivo)
                            
                            # Seguro anti-fallos: evitamos guardar la palabra "NOMBRE"
                            if len(nc) >= 1 and nc[0] != "NOMBRE":
                                nombre_final = nc[0].replace('_', ' ')
                                if len(nc) >= 4:
                                    apellido_final = nc[-2].replace('_', ' ')
                                elif len(nc) in (2, 3):
                                    apellido_final = nc[1].replace('_', ' ')
                                else:
                                    apellido_final = ""
                                print(f"¡Nombre SIAC (Negocio) procesado!: {nombre_final} {apellido_final}")
                                nombre_encontrado = True
                            break

                # B. ESTRATEGIA PARA PERSONAS (Fallback: Buscar "CONTACTO")
                if not nombre_encontrado and "DATOS" in texto_limpio and "CONTACTO" in texto_limpio:
                    lineas = [l.strip() for l in texto_limpio.split('\n') if len(l.strip()) > 2]
                    for i, linea in enumerate(lineas):
                        if "DATOS" in linea and "CONTACTO" in linea and len(lineas) > i+1:
                            linea_objetivo = lineas[i+1]
                            
                            etiquetas = ["NOMBRE", "NOMBRE:", "NOMBRES", "NOMBRES:", "CLIENTE", "CLIENTE:"]
                            saltos = 1
                            while linea_objetivo in etiquetas and (i + saltos + 1) < len(lineas):
                                saltos += 1
                                linea_objetivo = lineas[i+saltos]
                                
                            for etiqueta in ["NOMBRE:", "NOMBRE ", "CLIENTE:", "TITULAR:"]:
                                if linea_objetivo.startswith(etiqueta):
                                    linea_objetivo = linea_objetivo.replace(etiqueta, "").strip()

                            # Si la limpieza deja la línea vacía o es la palabra "NOMBRE", lo ignoramos
                            if not linea_objetivo or linea_objetivo == "NOMBRE":
                                continue

                            nc = self.agrupar_nombres_compuestos(linea_objetivo)
                            if len(nc) >= 1:
                                nombre_final = nc[0].replace('_', ' ')
                                if len(nc) >= 4:
                                    apellido_final = nc[-2].replace('_', ' ')
                                elif len(nc) in (2, 3):
                                    apellido_final = nc[1].replace('_', ' ')
                                else:
                                    apellido_final = ""
                                print(f"¡Nombre SIAC (Persona) procesado!: {nombre_final} {apellido_final}")
                                break

                # --- PASADA 2: LECTURA AGRESIVA (Solo para la INE) ---
                # Si vemos que es la INE, aplicamos el filtro para matar hologramas
                es_ine = "ELECTORAL" in texto_limpio or "INSTITUTO" in texto_limpio or "NACIONAL" in texto_limpio
                
                if es_ine and nombre_final == "DESC":
                    # Aplicamos el Adaptive Threshold solo a la INE
                    img_procesada = cv2.adaptiveThreshold(
                        gris_ampliado, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                        cv2.THRESH_BINARY, 31, 11
                    )
                    # Volvemos a leer la imagen pero ahora con el filtro aplicado
                    texto_ine = pytesseract.image_to_string(img_procesada, lang='spa').upper()
                    
                    lineas = [l.strip() for l in texto_ine.split('\n') if len(l.strip()) > 2]
                    for i, linea in enumerate(lineas):
                        if "NOMBRE" in linea or "N0MBRE" in linea or "NOM" in linea or "MBRE" in linea:
                            try:
                                ape1_raw = lineas[i+1] if len(lineas) > i+1 else ""
                                ape2_raw = lineas[i+2] if len(lineas) > i+2 else ""
                                nom_raw = lineas[i+3] if len(lineas) > i+3 else ""

                                palabras_fin = ["DOMICILIO", "CALLE", "C.", "AV", "EDAD", "SEXO", "CLAVE", "AÑO"]
                                es_domicilio = any(kw in nom_raw for kw in palabras_fin)

                                ape1_limpio = self.agrupar_nombres_compuestos(ape1_raw)
                                apellido_final = ape1_limpio[0].replace('_', ' ') if ape1_limpio else "DESC"

                                if es_domicilio or not nom_raw:
                                    nom_limpio = self.agrupar_nombres_compuestos(ape2_raw)
                                else:
                                    nom_limpio = self.agrupar_nombres_compuestos(nom_raw)

                                nombre_final = nom_limpio[0].replace('_', ' ') if nom_limpio else "DESC"
                                print(f"¡Nombre INE procesado!: {nombre_final} {apellido_final}")
                            except Exception as e:
                                print(f"Error procesando INE: {e}")
                            break

            # --- VALIDACIÓN FINAL ---
            if nombre_final == "DESC" or folio_final == "DESC":
                print(f"Alerta: Faltan datos (Folio: {folio_final}, Nombre: {nombre_final}). Mandando a revisión.")
                self.mover_a_revision(lista_archivos)
                return

            # --- CREACIÓN DE CARPETAS ---
            fecha_str = datetime.now().strftime("%A %d %B %Y").upper()
            carpeta_cliente = f"{folio_final}-{nombre_final} {apellido_final}-{capturista}".upper().strip("- ")
            
            ruta_final = os.path.join(RUTA_BASE, clave, fecha_str, carpeta_cliente)
            os.makedirs(ruta_final, exist_ok=True)

            for archivo in lista_archivos:
                shutil.move(archivo, os.path.join(ruta_final, os.path.basename(archivo)))
            
            notification.notify(
                title="Éxito",
                message=f"Carpeta creada:\n{carpeta_cliente}",
                app_name="Auditor Automático",
                timeout=3
            )

        except Exception as e:
            print(f"Error crítico procesando: {e}")
            self.mover_a_revision(lista_archivos)

    def mover_a_revision(self, lista_archivos):
        fecha_str = datetime.now().strftime("%Y-%m-%d")
        ruta_revision = os.path.join(RUTA_BASE, f"REVISION_MANUAL_{fecha_str}")
        os.makedirs(ruta_revision, exist_ok=True)
        
        for archivo in lista_archivos:
            try:
                shutil.move(archivo, os.path.join(ruta_revision, os.path.basename(archivo)))
            except Exception:
                pass
        
        notification.notify(
            title="Atención",
            message="Faltaron datos, archivos movidos a revisión manual",
            app_name="Auditor Automático",
            timeout=5
        )

# --- 3. EL PERRO GUARDIÁN CORREGIDO (WATCHDOG) ---
class VigilanteDescargas(FileSystemEventHandler):
    def __init__(self, procesador, app_gui):
        self.procesador = procesador
        self.app = app_gui
        self.archivos_pendientes = set()
        self.timer = None

    def procesar_ruta(self, ruta):
        nombre = os.path.basename(ruta)
        if nombre.startswith("WP_") and not nombre.endswith(('.tmp', '.crdownload', '.part')):
            if ruta not in self.archivos_pendientes:
                self.archivos_pendientes.add(ruta)
                print(f"Capturado: {nombre} ({len(self.archivos_pendientes)} archivos en búfer)")
                
                if self.timer: self.timer.cancel()
                self.timer = threading.Timer(4.0, self.verificar_y_procesar)
                self.timer.start()

    def on_created(self, event):
        if not event.is_directory: self.procesar_ruta(event.src_path)
    def on_moved(self, event):
        if not event.is_directory: self.procesar_ruta(event.dest_path)
    def on_modified(self, event):
        if not event.is_directory: self.procesar_ruta(event.src_path)

    def verificar_y_procesar(self):
        archivos = list(self.archivos_pendientes)
        self.archivos_pendientes.clear()
        
        if len(archivos) in [3, 4, 5]:
            notification.notify(
                title="Procesando",
                message=f"{len(archivos)} archivos detectados. Iniciando lectura...",
                app_name="Auditor Automático",
                timeout=2
            )
            clave = self.app.combo_clave.get()
            capturista = self.app.combo_capturista.get()
            
            time.sleep(1) 
            self.procesador.procesar_archivos(archivos, clave, capturista)
        elif len(archivos) > 0:
            print(f"Lote ignorado: Se detectaron {len(archivos)} archivos. No coincide con 3, 4 o 5.")

# --- 4. INTERFAZ GRÁFICA (GUI) ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Auditor Automático v{VERSION_LOCAL}")
        self.geometry("350x280")
        self.config = self.cargar_config()

        # --- NUEVO: Etiqueta de actualización oculta por defecto ---
        self.label_alerta = tk.Label(self, text="", fg="red", font=("Arial", 10, "bold"))
        self.label_alerta.pack(pady=(5, 0))

        tk.Label(self, text="Clave de Captura:").pack(pady=(10, 0))
        self.combo_clave = ttk.Combobox(self, values=self.config.get("claves", ["CLAVE-01"]))
        self.combo_clave.set(self.config.get("ultima_clave", ""))
        self.combo_clave.pack()

        tk.Label(self, text="Nombre del Capturista:").pack(pady=(10, 0))
        self.combo_capturista = ttk.Combobox(self, values=self.config.get("capturistas", ["JUAN PEREZ"]))
        self.combo_capturista.set(self.config.get("ultimo_capturista", ""))
        self.combo_capturista.pack()

        self.var_inicio = tk.BooleanVar(value=self.config.get("auto_inicio", False))
        tk.Checkbutton(self, text="Iniciar con Windows", variable=self.var_inicio, command=self.toggle_inicio).pack(pady=10)

        tk.Button(self, text="Guardar y Minimizar", command=self.guardar_y_minimizar).pack(pady=10)

        threading.Thread(target=self.revisar_actualizacion, daemon=True).start()

        self.procesador = ProcesadorDocumentos()
        self.vigilante = VigilanteDescargas(self.procesador, self)
        self.observer = Observer()
        self.observer.schedule(self.vigilante, RUTA_DESCARGAS, recursive=False)
        self.observer.start()

        self.protocol("WM_DELETE_WINDOW", self.cerrar_app)

    def revisar_actualizacion(self):
        try:
            respuesta = requests.get(URL_VERSION_GITHUB, timeout=5)
            version_nube = float(respuesta.text.strip())
            
            if version_nube > VERSION_LOCAL:
                # Usamos self.after para que la ventana emergente no congele el hilo secundario
                self.after(0, self.preguntar_actualizacion, version_nube)
        except Exception as e:
            print("No se pudo verificar la actualización:", e)

    def preguntar_actualizacion(self, version_nube):
        # Muestra la ventana emergente de Aceptar / Rechazar
        respuesta = messagebox.askyesno(
            "¡Actualización Disponible!",
            f"Se ha detectado la versión {version_nube} en el servidor.\n\n"
            f"¿Deseas descargarla e instalarla ahora? El programa se reiniciará."
        )
        if respuesta: # Si el usuario le dio a "Sí"
            self.ejecutar_actualizacion()

    def ejecutar_actualizacion(self):
        try:
            self.label_alerta = tk.Label(self, text="Descargando actualización...", fg="blue", font=("Arial", 10, "bold"))
            self.label_alerta.pack(pady=5)
            self.update() # Refresca la interfaz

            # 1. Descargamos el nuevo código de GitHub
            nuevo_codigo = requests.get(URL_CODIGO_GITHUB, timeout=10).text

            # 2. Lo guardamos en un archivo temporal
            archivo_temp = "auditor_temp.py"
            with open(archivo_temp, "w", encoding="utf-8") as f:
                f.write(nuevo_codigo)

            # 3. Preparamos las rutas para el .bat
            archivo_actual = os.path.abspath(__file__)
            archivo_bat = "actualizador.bat"
            ruta_python = sys.executable

            # 4. Creamos el script .bat que hará el trabajo sucio
            # (Espera 2 seg, sobreescribe el archivo viejo, abre el nuevo, y se borra a sí mismo)
            codigo_bat = f"""@echo off
timeout /t 2 /nobreak > NUL
move /Y "{archivo_temp}" "{archivo_actual}"
start "" "{ruta_python}" "{archivo_actual}"
del "%~f0"
"""
            with open(archivo_bat, "w", encoding="utf-8") as f:
                f.write(codigo_bat)

            # 5. Ejecutamos el .bat en segundo plano y cerramos el programa actual
            subprocess.Popen([archivo_bat], shell=True)
            self.cerrar_app()
            sys.exit()

        except Exception as e:
            messagebox.showerror("Error de Actualización", f"No se pudo actualizar:\n{e} \nPor favor avisa a soporte técnico.")

    def cargar_config(self):
        if os.path.exists(ARCHIVO_CONFIG):
            with open(ARCHIVO_CONFIG, 'r') as f: return json.load(f)
        return {"claves": [], "capturistas": []}

    def guardar_config(self):
        clave_actual = self.combo_clave.get().upper()
        capturista_actual = self.combo_capturista.get().upper()
        claves = list(self.combo_clave['values'])
        if clave_actual and clave_actual not in claves: claves.append(clave_actual)
        capturistas = list(self.combo_capturista['values'])
        if capturista_actual and capturista_actual not in capturistas: capturistas.append(capturista_actual)

        nueva_config = {
            "claves": claves, "capturistas": capturistas,
            "ultima_clave": clave_actual, "ultimo_capturista": capturista_actual,
            "auto_inicio": self.var_inicio.get()
        }
        with open(ARCHIVO_CONFIG, 'w') as f: json.dump(nueva_config, f)

    def toggle_inicio(self):
        key = winreg.HKEY_CURRENT_USER
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            registro = winreg.OpenKey(key, key_path, 0, winreg.KEY_ALL_ACCESS)
            if self.var_inicio.get():
                winreg.SetValueEx(registro, "AuditorWhatsApp", 0, winreg.REG_SZ, sys.executable + ' "' + os.path.abspath(__file__) + '"')
            else:
                winreg.DeleteValue(registro, "AuditorWhatsApp")
            winreg.CloseKey(registro)
        except Exception as e:
            print("Error en registro:", e)

    def guardar_y_minimizar(self):
        self.guardar_config()
        self.iconify() 

    def cerrar_app(self):
        self.guardar_config()
        self.observer.stop()
        self.observer.join()
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()