"""
================================================================================
DOCUMENTACIÓN GENERAL: Scanner de CCTV (Hikvision ISAPI / Dahua CGI)
================================================================================
--------------------------------------------------------------------------------
1. REQUISITOS DE INSTALACIÓN
--------------------------------------------------------------------------------
Python 3.8 o superior  →  https://www.python.org/downloads/
  En Windows: marcar "Add Python to PATH" durante la instalación.

Paquetes externos:
  - requests (OBLIGATORIO) : comunicación HTTP/HTTPS con NVRs y cámaras.
  - urllib3                : se instala automáticamente como dependencia de requests.
  - keyring (OPCIONAL)     : almacenamiento seguro de credenciales en el S.O.

Instalación rápida:
    pip install requests         (Instalación mínima)
    pip install requests keyring (Recomendado para PC de escritorio)

Si "keyring" no está instalado (ej. servidores Linux), el script seguirá funcionando 
pidiendo la contraseña de forma manual o leyéndola del archivo JSON.

--------------------------------------------------------------------------------
2. FUNCIONAMIENTO DEL SCRIPT
--------------------------------------------------------------------------------
El flujo lógico del programa se ejecuta en los siguientes pasos:

1. Lectura de Credenciales y Parámetros:
   - Intenta leer configuración desde el archivo "CCTV-Scanner-config.json".
   - Si no lo encuentra, activa el Modo Interactivo pidiendo los datos por consola.
   - Orden de prioridad de contraseñas: 
     1º Archivo JSON -> 2º Keyring (si está instalado) -> 3º Ingreso manual.

2. Fase 1 - Extracción desde NVRs (asíncrona): 
   Se conecta a los NVRs en paralelo mediante ISAPI para extraer los canales IP, 
   descubriendo las IPs internas de cada cámara, su canal y su descripción.

3. Fase 2 - Escaneo Directo Asíncrono (Solo si se elige "Modo Completo"): 
   Consulta de forma unicast a cada IP descubierta en la Fase 1.
   Detección Ciega (Multimarca): El script primero intenta comunicarse con el 
   protocolo de Hikvision (ISAPI). Si falla, intenta con Dahua (CGI) y como 
   último recurso intenta con el protocolo Legacy de Hikvision (PSIA).
   Soporta automáticamente autenticación moderna (Digest) y antigua (Basic).

4. Almacenamiento de Reportes: 
   Genera la carpeta "Datos" y guarda dentro:
   - "Datos/cctv_online.json" con la información técnica completa.
   - "Datos/cctv_offline.log" con el detalle de equipos caídos o inaccesibles.

--------------------------------------------------------------------------------
3. ESTRUCTURA DE CCTV-Scanner-config.json (Debe estar junto al script)
--------------------------------------------------------------------------------
{
    "nvr_user": "apinfo",
    "nvr_pass": "TuClaveSecreta123", 
    "opcion_puerto": "3",
    "max_workers": 50,
    "tipo_escaneo": "2",
    "nvrs": [
        {"ip": "192.168.1.100"},
        {"ip": "10.0.0.5"}
    ]
}

Explicación de los Campos Clave:
- nvr_pass (Opcional): Ideal para automatizar el script en servidores sin Keyring.
- opcion_puerto (String) -> Control de TLS para optimizar velocidad:
  "1": Fallback Total (TLS 1.3 -> 1.2 -> 1.0 -> HTTP).
  "2": Estándar Legacy (TLS 1.2 -> 1.0 -> HTTP).
  "3": Rápido / Directo (TLS 1.2 -> HTTP) [RECOMENDADO].
  "4": Solo HTTPS Estricto (TLS 1.3 -> 1.2 -> 1.0).
  "5": Solo HTTP (80) + Compatibilidad Cámaras Antiguas.
- max_workers: Cantidad de hilos (Recomendado 10-20 en redes normales).
- tipo_escaneo: "1" (Solo NVRs, muy rápido) o "2" (Completo Unicast).
- nvrs: direcciones ip de los nvr a scanear
================================================================================
"""

import os
import sys
import ssl
import xml.etree.ElementTree as ET
import json
import re
import concurrent.futures
import getpass
import ipaddress
import time

# --- GESTIÓN DE DEPENDENCIAS OBLIGATORIAS ---
try:
    import requests
    import urllib3
    from requests.adapters import HTTPAdapter
    from requests.auth import HTTPDigestAuth, HTTPBasicAuth
except ImportError as e:
    print("\n" + "="*70)
    print(" [ERROR FATAL] Falta la dependencia 'requests' para ejecutar el script.")
    print("="*70)
    print(f" Detalle técnico: {e}")
    print("\n Para solucionarlo, abrí tu terminal y ejecutá según tu sistema operativo:\n")
    print(" Windows:")
    print("      pip install requests\n")
    print(" macOS / Linux:")
    print("      pip3 install requests\n")
    print("="*70 + "\n")
    input(" Presioná Enter para salir...")
    sys.exit(1)

# --- GESTIÓN DE DEPENDENCIAS OPCIONALES ---
try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False
    print("\n[INFO] La librería 'keyring' no está instalada. El script funcionará,")
    print("       pero el almacenamiento seguro de contraseñas en el S.O. estará desactivado.")

try:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except NameError:
    pass

# Variables de rutas
ARCHIVO_CONFIG = "CCTV-Scanner-config.json"
CARPETA_DATOS  = "Datos"

# Configuración de Timeouts: (Conexión TCP, Tiempo de Lectura/Procesamiento)
T_OUT = (3.0, 10.0)

class TLS13Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

class ModernTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except AttributeError:
            pass
        try:
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        except ssl.SSLError:
            pass
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

_TLS13_SUPPORTED = hasattr(ssl, 'TLSVersion') and hasattr(ssl.TLSVersion, 'TLSv1_3')

ADAPTERS = {
    "https_modern": ModernTLSAdapter(),
    "https_legacy": LegacyTLSAdapter(),
    "http":         HTTPAdapter(),
}
if _TLS13_SUPPORTED:
    ADAPTERS["https_tls13"] = TLS13Adapter()

_PROTO_LABEL = {
    "https_tls13":  "HTTPS/TLS1.3",
    "https_modern": "HTTPS/TLS1.2",
    "https_legacy": "HTTPS/TLS1.0",
    "http":         "HTTP",
}

def describir_protocolo(key):
    return _PROTO_LABEL.get(key, key.upper())

_HTTPS_CHAIN = (
    [("https_tls13", "443")] if _TLS13_SUPPORTED else []
) + [("https_modern", "443"), ("https_legacy", "443")]

# ---------------------------------------------------------------------------
# GESTIÓN DE CONFIGURACIÓN
# ---------------------------------------------------------------------------

def cargar_config_json():
    if os.path.exists(ARCHIVO_CONFIG):
        try:
            with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Error leyendo '{ARCHIVO_CONFIG}': {e}")
    return None

def pedir_tipo_escaneo():
    print("\n  ┌── Tipo de Escaneo ──────────────────────────────┐")
    print("  │  1 · Básico (Solo canales, IPs y desc. del NVR) │")
    print("  │  2 · Completo (Consulta unicast a cada cámara)  │")
    print("  └─────────────────────────────────────────────────┘")
    while True:
        opcion = input("  Elegí una opción [1/2, Enter=2]: ").strip()
        if opcion == "":
            return "2"
        if opcion in ["1", "2"]:
            return opcion
        print("  [!] Ingresá 1 o 2.")

def pedir_puertos():
    OPCIONES = {
        "1": _HTTPS_CHAIN + [("http", "80")],
        "2": [("https_modern", "443"), ("https_legacy", "443"), ("http", "80")],
        "3": [("https_modern", "443"), ("http", "80")],
        "4": _HTTPS_CHAIN,
        "5": [("http", "80")]
    }
    print("\n  ┌── Nivel de Seguridad y Fallback (NVRs y Cámaras) ───────┐")
    print("  │  1 · Fallback Total (TLS 1.3 -> 1.2 -> 1.0 -> HTTP)     │")
    print("  │  2 · Estándar Legacy (TLS 1.2 -> 1.0 -> HTTP)           │")
    print("  │  3 · Rápido / Directo (TLS 1.2 -> HTTP) [Recomendado]   │")
    print("  │  4 · Solo HTTPS Estricto (TLS 1.3 -> 1.2 -> 1.0)        │")
    print("  │  5 · Solo HTTP (80) + Compatibilidad Cámaras Antiguas   │")
    print("  └─────────────────────────────────────────────────────────┘")

    while True:
        opcion = input("  Elegí una opción [1-5, Enter=3]: ").strip()
        if opcion == "":
            return OPCIONES["3"]
        if opcion in OPCIONES:
            return OPCIONES[opcion]
        print("  [!] Ingresá un número del 1 al 5.")

def pedir_workers():
    MAX_PERMITIDO = 50
    print("\n  ┌── Hilos de Ejecución (Workers) ─────────────────┐")
    print(f"  │ Rango permitido : 1 – {MAX_PERMITIDO:<25} │")
    print("  │ Recomendado     : 10–20 en redes normales       │")
    print("  └─────────────────────────────────────────────────┘")

    while True:
        try:
            valor = input("  ¿Cuántos hilos abrir al mismo tiempo? [Enter = 50]: ").strip()
            if valor == "":
                return 50
            workers = int(valor)
            if 1 <= workers <= MAX_PERMITIDO:
                return workers
            print(f"  [!] Ingresá un número entre 1 y {MAX_PERMITIDO}.")
        except ValueError:
            print("  [!] Ingresá solo un número entero.")

# ---------------------------------------------------------------------------
# FASE 1: Extracción desde NVRs
# ---------------------------------------------------------------------------

def procesar_nvr(args):
    nvr, puertos, user, password = args

    nvr_name         = "NVR_Desconocido"
    nvr_modelo       = "N/A"
    nvr_serial       = "N/A"
    nvr_mac          = "N/A"
    nvr_firmware     = "N/A"
    camaras_nvr_list = []

    for protocolo_key, puerto in puertos:
        proto_real = "https" if protocolo_key.startswith("https") else "http"
        session = requests.Session()
        session.mount(f"{proto_real}://", ADAPTERS[protocolo_key])

        try:
            url_info = f"{proto_real}://{nvr['ip']}:{puerto}/ISAPI/System/deviceInfo"
            resp_info = session.get(
                url_info, auth=HTTPDigestAuth(user, password),
                timeout=T_OUT, verify=False
            )
            # Si el NVR es legacy y requiere BasicAuth en lugar de Digest
            if resp_info.status_code == 401:
                resp_info = session.get(url_info, auth=HTTPBasicAuth(user, password), timeout=T_OUT, verify=False)

            if resp_info.status_code == 200:
                xml_info  = re.sub(' xmlns="[^"]+"', '', resp_info.text)
                root_info = ET.fromstring(xml_info)

                def _f(tag):
                    el = root_info.find(tag)
                    return el.text if el is not None else "N/A"

                nvr_name     = _f('deviceName') or "NVR_Desconocido"
                nvr_modelo   = _f('model')
                serial_raw   = _f('serialNumber')
                nvr_serial   = serial_raw[len(nvr_modelo):] if (serial_raw != "N/A" and serial_raw.startswith(nvr_modelo)) else serial_raw
                nvr_mac      = _f('macAddress')
                nvr_firmware = _f('firmwareVersion')

            etiqueta      = " (Fallback)" if (protocolo_key, puerto) != puertos[0] else ""
            proto_display = describir_protocolo(protocolo_key)
            print(f"Consultando NVR: {nvr_name} ({nvr['ip']}) en {proto_display}:{puerto}{etiqueta}...")

            url_cameras = f"{proto_real}://{nvr['ip']}:{puerto}/ISAPI/ContentMgmt/InputProxy/channels"
            response = session.get(
                url_cameras, auth=HTTPDigestAuth(user, password),
                timeout=T_OUT, verify=False
            )
            if response.status_code == 401:
                response = session.get(url_cameras, auth=HTTPBasicAuth(user, password), timeout=T_OUT, verify=False)

            response.raise_for_status()
            xml_data = re.sub(' xmlns="[^"]+"', '', response.text)
            root = ET.fromstring(xml_data)

            camaras_nvr = 0
            for channel in root.findall('InputProxyChannel'):
                chan_id_elem = channel.find('id')
                chan_id = chan_id_elem.text if chan_id_elem is not None else "N/A"
                
                chan_name_elem = channel.find('name')
                camera_desc = chan_name_elem.text if (chan_name_elem is not None and chan_name_elem.text is not None) else "N/A"
                
                # PARCHE PARA CARACTERES RAROS DEL NVR
                if camera_desc != "N/A":
                    camera_desc = camera_desc.replace("~N", "Ñ").replace("~n", "ñ")

                ip_address = "N/A"
                descriptor = channel.find('sourceInputPortDescriptor')
                if descriptor is not None:
                    ip_elem = descriptor.find('ipAddress')
                    if ip_elem is not None and ip_elem.text:
                        ip_address = ip_elem.text

                if ip_address and ip_address != "0.0.0.0" and ip_address != "N/A":
                    camaras_nvr_list.append({
                        "ip_address": ip_address,
                        "camera_name": camera_desc,
                        "channel_id": int(chan_id) if chan_id.isdigit() else chan_id,
                        "nvr_ip":     nvr['ip'],
                        "nvr_name":   nvr_name,
                        "origen_datos": "NVR (Modo Básico)"
                    })
                    camaras_nvr += 1

            nvr_info = {
                "ip":                nvr['ip'],
                "nvr_name":          nvr_name,
                "modelo":            nvr_modelo,
                "nro_serie":         nvr_serial,
                "mac_address":       nvr_mac,
                "firmware":          nvr_firmware,
                "protocolo_conexion": f"{describir_protocolo(protocolo_key)}:{puerto}",
                "total_camaras":     camaras_nvr,
            }
            print(f"  -> OK: {camaras_nvr} cámaras extraídas de {nvr['ip']}.")
            return camaras_nvr_list, nvr_info

        except Exception as e:
            if (protocolo_key, puerto) != puertos[-1]:
                print(f"  -> [WARN] NVR {nvr['ip']} no respondió en {describir_protocolo(protocolo_key)}:{puerto}. Probando siguiente...")
            else:
                print(f"  -> [ERROR] Fallo total al consultar canales en NVR {nvr['ip']}: {e}")

    return [], {
        "ip":                nvr['ip'],
        "nvr_name":          "Fallo/Offline",
        "modelo":            "N/A",
        "nro_serie":         "N/A",
        "mac_address":       "N/A",
        "firmware":          "N/A",
        "protocolo_conexion": "Fallo/Offline",
        "total_camaras":     0,
    }


def obtener_camaras_desde_nvrs(nvr_list, puertos, user, password, max_workers=5):
    if not nvr_list:
        return [], []

    workers_nvr = min(max_workers, len(nvr_list))
    print(f"\n--- FASE 1: Extrayendo canales desde los NVRs ({workers_nvr} hilos) ---")

    args_list = [(nvr, puertos, user, password) for nvr in nvr_list]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers_nvr) as executor:
        resultados = list(executor.map(procesar_nvr, args_list))

    camaras_base = []
    nvrs_info    = []
    for camaras_nvr, nvr_info in resultados:
        camaras_base.extend(camaras_nvr)
        nvrs_info.append(nvr_info)

    print(f"-> Total de cámaras encontradas en los NVRs: {len(camaras_base)}")
    return camaras_base, nvrs_info

# ---------------------------------------------------------------------------
# FASE 2: Extracción directa a cada cámara (Fallback ISAPI -> CGI -> PSIA)
# ---------------------------------------------------------------------------

def intentar_conexion(ip, protocolo_key, puerto, user, password):
    proto_real = "https" if protocolo_key.startswith("https") else "http"
    session = requests.Session()
    session.mount(f"{proto_real}://", ADAPTERS[protocolo_key])

    # Función auxiliar para probar Digest Auth y, si rechaza, intentar Basic Auth
    def get_auth_robusto(url_test):
        r = session.get(url_test, auth=HTTPDigestAuth(user, password), timeout=T_OUT, verify=False)
        if r.status_code == 401:
            r = session.get(url_test, auth=HTTPBasicAuth(user, password), timeout=T_OUT, verify=False)
        return r

    # 1er Intento: Hikvision (ISAPI)
    try:
        response = get_auth_robusto(f"{proto_real}://{ip}:{puerto}/ISAPI/System/deviceInfo")
        if response.status_code == 200:
            return response, "Hikvision"
    except requests.exceptions.RequestException:
        pass 

    time.sleep(1.0) # Micro-descanso 

    # 2do Intento: Dahua (CGI API)
    try:
        response = get_auth_robusto(f"{proto_real}://{ip}:{puerto}/cgi-bin/magicBox.cgi?action=getSystemInfo")
        if response.status_code == 200:
            return response, "Dahua"
    except requests.exceptions.RequestException:
        pass

    time.sleep(1.0)

    # 3er Intento: Hikvision Reliquia (PSIA)
    try:
        response = get_auth_robusto(f"{proto_real}://{ip}:{puerto}/PSIA/System/deviceInfo")
        if response.status_code == 200:
            # Si responde, lo tratamos como Hikvision porque la estructura XML es idéntica
            return response, "Hikvision" 
    except requests.exceptions.RequestException:
        pass

    raise requests.exceptions.ConnectionError(f"[{ip}] No responde a ISAPI, CGI ni PSIA.")

def procesar_camara(args):
    cam_data, puertos, user, password = args
    ip = cam_data['ip_address']

    response     = None
    marca_detect = "N/A"
    protocolo_ok = None
    puerto_ok    = None

    for protocolo_key, puerto in puertos:
        try:
            response, marca_detect = intentar_conexion(ip, protocolo_key, puerto, user, password)
            protocolo_ok = protocolo_key
            puerto_ok    = puerto
            etiqueta      = "(fallback)" if (protocolo_key, puerto) != puertos[0] else ""
            proto_display = describir_protocolo(protocolo_key)
            print(f"[OK] {ip} ({marca_detect}) → {proto_display}:{puerto} {etiqueta}".strip())
            break
        except requests.exceptions.RequestException:
            if (protocolo_key, puerto) != puertos[-1]:
                print(f"[WARN] {ip} no respondió en {describir_protocolo(protocolo_key)}:{puerto}. Probando siguiente...")

    camera_name       = cam_data.get("camera_name", "N/A")
    mac_address       = "N/A"
    modelo            = "N/A"
    nro_serie         = "N/A"
    firmware          = "N/A"
    protocolo_conexion = "Fallo/Offline"

    if response is not None:
        protocolo_conexion = f"{describir_protocolo(protocolo_ok)}:{puerto_ok}"
        
        # --- PARSEO HIKVISION / PSIA (XML) ---
        if marca_detect == "Hikvision":
            try:
                # Limpiamos el namespace sea cual sea (ISAPI o PSIA)
                xml_info  = re.sub(' xmlns="[^"]+"', '', response.text)
                root_info = ET.fromstring(xml_info)

                def texto(tag):
                    el = root_info.find(tag)
                    return el.text if el is not None else "N/A"

                modelo     = texto('model')
                serial_raw = texto('serialNumber')
                
                if serial_raw != "N/A" and serial_raw.startswith(modelo):
                    nro_serie = serial_raw[len(modelo):]
                else:
                    nro_serie = serial_raw
                
                camera_name_real = texto('deviceName')
                if camera_name_real is not None and camera_name_real != "N/A": 
                    # PARCHE
                    camera_name = camera_name_real.replace("~N", "Ñ").replace("~n", "ñ")
                    
                mac_address = texto('macAddress')
                firmware    = texto('firmwareVersion')
            except Exception as e:
                print(f"[ERROR XML] Fallo al leer datos Hikvision de {ip}: {e}")

        # --- PARSEO DAHUA (Texto Plano Clave=Valor) ---
        elif marca_detect == "Dahua":
            try:
                texto_resp = response.text

                def extraer_dahua(clave, texto):
                    match = re.search(rf"{clave}=(.*)", texto, re.IGNORECASE)
                    return match.group(1).strip() if match else "N/A"

                modelo    = extraer_dahua("deviceType", texto_resp)
                nro_serie = extraer_dahua("serialNumber", texto_resp)
                
                mac_raw = extraer_dahua("macAddress", texto_resp) 
                if mac_raw != "N/A":
                    mac_address = mac_raw

                proto_real = "https" if protocolo_ok.startswith("https") else "http"

                def dahua_extra_request(url):
                    r = requests.get(url, auth=HTTPDigestAuth(user, password), timeout=T_OUT, verify=False)
                    if r.status_code == 401:
                        r = requests.get(url, auth=HTTPBasicAuth(user, password), timeout=T_OUT, verify=False)
                    return r

                # Consulta exclusiva para Firmware
                try:
                    url_fw = f"{proto_real}://{ip}:{puerto_ok}/cgi-bin/magicBox.cgi?action=getSoftwareVersion"
                    resp_fw = dahua_extra_request(url_fw)
                    if resp_fw.status_code == 200:
                        fw_raw = extraer_dahua("version", resp_fw.text)
                        if fw_raw != "N/A":
                            firmware = fw_raw
                except Exception:
                    pass

                # Consulta exclusiva para MAC Address
                if mac_address == "N/A":
                    try:
                        url_mac = f"{proto_real}://{ip}:{puerto_ok}/cgi-bin/configManager.cgi?action=getConfig&name=Network"
                        resp_mac = dahua_extra_request(url_mac)
                        if resp_mac.status_code == 200:
                            mac_net = extraer_dahua("PhysicalAddress", resp_mac.text)
                            if mac_net != "N/A":
                                mac_address = mac_net
                    except Exception:
                        pass

            except Exception as e:
                print(f"[ERROR CGI] Fallo al leer datos Dahua de {ip}: {e}")

    else:
        intentados = ", ".join(f"{describir_protocolo(p)}:{pt}" for p, pt in puertos)
        print(f"[ERROR] {ip} no respondió en ningún puerto ({intentados}).")

    camara_ordenada = {
        "ip_address":        cam_data["ip_address"],
        "camera_name":       camera_name,
        "mac_address":       mac_address,
        "modelo":            modelo,
        "nro_serie":         nro_serie,
        "firmware":          firmware,
        "protocolo_conexion": protocolo_conexion,
        "nvr_name":          cam_data["nvr_name"],
        "nvr_ip":            cam_data["nvr_ip"],
        "channel_id":        cam_data["channel_id"],
    }

    return camara_ordenada

# ---------------------------------------------------------------------------
# MAIN - Orquestador
# ---------------------------------------------------------------------------

def ejecutar_escaneo_unificado():
    config_data      = cargar_config_json()
    usar_interactivo = (config_data is None)

    print("\n=========================================")
    print("             ESCANEO CCTV ISAPI           ")
    print("=========================================")

    if usar_interactivo:
        print(f"\n[INFO] No se detectó el archivo de configuración '{ARCHIVO_CONFIG}'.")
        ver_ayuda = input("  ¿Querés ver la documentación de uso antes de continuar? [s/n, Enter=n]: ").strip().lower()
        if ver_ayuda == 's':
            print(__doc__)
            print("="*80)
            input("Presioná Enter para continuar con el asistente manual...")

    user = None
    password = None

    # 1. Intentar sacar User y Pass directamente del JSON
    if config_data:
        user = str(config_data.get("nvr_user", "")).strip() or None
        password = str(config_data.get("nvr_pass", "")).strip() or None

    if not user:
        print("\n[!] No se detectó usuario en la configuración.")
        user = input("  -> Ingresá el usuario de los NVRs/Cámaras: ").strip()
        if not user:
            print("[ERROR] El usuario no puede estar vacío. Saliendo.")
            return
        usar_interactivo = True

    # 2. Si no hay contraseña en el JSON, buscarla con Keyring o manualmente
    if not password:
        # Intento A: Leer de Keyring (solo si está instalado)
        if HAS_KEYRING:
            password = keyring.get_password("CCTV_Daemon", user)

        # Intento B: Si Keyring falló o no está instalado, pedir a mano
        if not password:
            if HAS_KEYRING:
                print(f"\n[INFO] No hay credenciales guardadas en el S.O. para '{user}'.")
                
            password = getpass.getpass(f"  -> Ingresá la contraseña para '{user}': ").strip()

            if not password:
                print("[ERROR] La contraseña no puede estar vacía. Saliendo.")
                return
            usar_interactivo = True

            # Preguntar para guardar en el S.O. (solo si Keyring está disponible)
            if HAS_KEYRING:
                guardar = input(f"  ¿Querés guardar esta clave en el S.O. para la próxima? [s/n]: ").strip().lower()
                if guardar == 's':
                    try:
                        keyring.set_password("CCTV_Daemon", user, password)
                        print("[OK] Contraseña guardada.")
                    except Exception as e:
                        print(f"[WARN] No se pudo guardar la clave: {e}")

    nvr_list = []
    if config_data and "nvrs" in config_data:
        nvr_list = config_data["nvrs"]

    if not nvr_list:
        print(f"\n[INFO] No se encontró una lista de NVRs.")
        while True:
            print("  Ingresá las IPs de los NVRs separadas por coma")
            ips_input = input("  -> Ejemplo [192.168.1.100, 192.168.1.101]: ").strip()

            if not ips_input:
                print("  [ERROR] No ingresaste ninguna IP. Intentá de nuevo.\n")
                continue

            ips_crudas = [ip.strip() for ip in ips_input.split(",") if ip.strip()]
            nvr_list_temp = []
            errores_ip = False

            for ip_str in ips_crudas:
                try:
                    ipaddress.ip_address(ip_str)
                    nvr_list_temp.append({"ip": ip_str})
                except ValueError:
                    print(f"  [!] FORMATO INVÁLIDO: '{ip_str}' no es una IP real.")
                    errores_ip = True

            if errores_ip:
                print("  [!] Ingresá la lista nuevamente y sin errores.\n")
            else:
                nvr_list = nvr_list_temp
                break
                
        usar_interactivo = True

    tipo_escaneo = None
    puertos      = None
    max_workers  = None

    OPCIONES_PUERTOS = {
        "1": _HTTPS_CHAIN + [("http", "80")],
        "2": [("https_modern", "443"), ("https_legacy", "443"), ("http", "80")],
        "3": [("https_modern", "443"), ("http", "80")],
        "4": _HTTPS_CHAIN,
        "5": [("http", "80")]
    }

    if not usar_interactivo:
        te = str(config_data.get("tipo_escaneo", "")).strip()
        op = str(config_data.get("opcion_puerto", "")).strip()
        mw = config_data.get("max_workers")
        
        if te in ["1", "2"] and op in OPCIONES_PUERTOS and isinstance(mw, int) and 1 <= mw <= 50:
            tipo_escaneo = te
            puertos      = OPCIONES_PUERTOS[op]
            max_workers  = mw
            print(f"\n[INFO] Configuración cargada automáticamente desde '{ARCHIVO_CONFIG}'.")
        else:
            print(f"\n[WARN] Parámetros incompletos o inválidos en JSON. Pasando a modo manual...")
            usar_interactivo = True

    if usar_interactivo or tipo_escaneo is None:
        tipo_escaneo = pedir_tipo_escaneo()

    if usar_interactivo or puertos is None:
        puertos = pedir_puertos()

    if usar_interactivo or max_workers is None:
        max_workers = pedir_workers()

    camaras_base, nvrs_info = obtener_camaras_desde_nvrs(nvr_list, puertos, user, password, max_workers)
    
    if not camaras_base:
        print("\n[ERROR] No se pudieron extraer canales de los NVRs provistos. Saliendo.")
        return

    camaras_exitosas = []
    camaras_fallidas = []

    if tipo_escaneo == "1":
        print("\n[INFO] Modo Básico: Omitiendo Fase 2 (consulta unicast a cámaras).")
        camaras_exitosas = camaras_base
    else:
        desc_puertos = " → ".join(f"{describir_protocolo(p)}:{pt}" for p, pt in puertos)
        print(f"\n--- FASE 2: Escaneo asíncrono ({max_workers} simultáneas · {desc_puertos}) ---\n")

        args = [(cam, puertos, user, password) for cam in camaras_base]
        camaras_completas = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for resultado in executor.map(procesar_camara, args):
                camaras_completas.append(resultado)

        camaras_exitosas = [c for c in camaras_completas if c["protocolo_conexion"] != "Fallo/Offline"]
        camaras_fallidas = [c for c in camaras_completas if c["protocolo_conexion"] == "Fallo/Offline"]

    os.makedirs(CARPETA_DATOS, exist_ok=True)

    archivo_salida = os.path.join(CARPETA_DATOS, "cctv_online.json")
    with open(archivo_salida, "w", encoding="utf-8") as f:
        json.dump({"nvrs": nvrs_info, "camaras": camaras_exitosas}, f, indent=4, ensure_ascii=False)

    archivo_log = os.path.join(CARPETA_DATOS, "cctv_offline.log")
    with open(archivo_log, "w", encoding="utf-8") as f_log:
        if tipo_escaneo == "1":
            f_log.write("=== LOG VACÍO ===\nAl ejecutar un Escaneo Básico (Solo NVRs), no se puede determinar qué cámaras están realmente offline en la red.")
        elif camaras_fallidas:
            f_log.write("=== CÁMARAS QUE NO RESPONDIERON AL ESCANEO DIRECTO ===\n")
            for cam in camaras_fallidas:
                f_log.write(f"IP: {cam['ip_address']} | Proveniente del NVR: {cam['nvr_name']} ({cam['nvr_ip']}) | Canal NVR: {cam['channel_id']}\n")
        else:
            f_log.write("Todas las cámaras respondieron correctamente de forma directa. ¡0 fallas!")

    print(f"\n--- RESUMEN ---")
    print(f"Total NVRs procesados               : {len(nvr_list)}")
    print(f"Total canales identificados en NVRs : {len(camaras_base)}")
    if tipo_escaneo == "2":
        print(f"Cámaras online (Responden Unicast)  : {len(camaras_exitosas)}")
        print(f"Cámaras offline (Log de errores)    : {len(camaras_fallidas)}")
    else:
        print(f"Cámaras extraídas en Modo Básico    : {len(camaras_exitosas)}")
        
    print(f"-> Archivo generado                 : '{archivo_salida}'")
    print(f"-> Log de errores generado          : '{archivo_log}'")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ["--help", "-h", "--ayuda"]:
        print(__doc__)
        print("="*80)
        input("Presioná Enter para cerrar...")
        sys.exit(0)

    try:
        ejecutar_escaneo_unificado()
    except Exception as e:
        import traceback
        print(f"\nOcurrió un error grave: {e}")

    print("\n" + "="*40)
    input("Presiona Enter para cerrar la ventana...")