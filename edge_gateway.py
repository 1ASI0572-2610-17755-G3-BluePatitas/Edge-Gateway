import os
import re
import sqlite3
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==============================================================================
# CONFIGURACIÓN DE PUERTOS Y ENLACES (Arquitectura C4)
# ==============================================================================
BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend-bluepatitas.onrender.com").rstrip("/")
URL_NUBE_TELEMETRIA = f"{BACKEND_URL}/api/monitoring/telemetry"

# Variable en memoria para controlar la orden del motor
alimentar_pendiente = False

# Variables en memoria para controlar el horario de dispensación automática
horario_activo = False
horario_intervalo_segundos = 60
ultimo_envio_horario = 0

# Variables para simular collar GPS (De origen estático a movimiento en el Borde)
simulacion_activa = False
base_latitude = -12.046374
base_longitude = -77.042793
offset_latitude = 0.0
offset_longitude = 0.0
active_target_id = None

import math


def distance_meters(lat1, lon1, lat2, lon2):
    R = 6371000  # radius of Earth in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def parse_intervalo_segundos(intervalo_str):
    if not intervalo_str:
        return 60

    # Buscar segundos
    match_seg = re.search(r"(\d+)\s*(?:segundo|sec|s)", intervalo_str, re.IGNORECASE)
    if match_seg:
        return int(match_seg.group(1))

    # Buscar minutos
    match_min = re.search(r"(\d+)\s*(?:minuto|min|m)", intervalo_str, re.IGNORECASE)
    if match_min:
        return int(match_min.group(1)) * 60

    # Buscar horas
    match_hr = re.search(r"(\d+)\s*(?:hora|hr|h)", intervalo_str, re.IGNORECASE)
    if match_hr:
        return int(match_hr.group(1)) * 3600

    return 60


# ==============================================================================
# 1. INICIALIZACIÓN DE LA BASE DE DATOS LOCAL (SQLite)
# ==============================================================================
def init_database():
    conn = sqlite3.connect("edge_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            temperatura REAL NOT NULL,
            humedad REAL NOT NULL,
            fecha_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
            sincronizado INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ubicaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitud REAL NOT NULL,
            longitud REAL NOT NULL,
            fecha_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
            sincronizado INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("[BD Local] Caché SQLite inicializada en el Edge.")


def obtener_target_id():
    global base_latitude, base_longitude, active_target_id

    # 1. Si el targetId fue configurado explícitamente, usarlo
    if active_target_id:
        return active_target_id

    # 2. De lo contrario, buscar dinámicamente la zona más cercana a la posición de simulación
    try:
        response = requests.get(
            f"{BACKEND_URL}/api/monitoring/zones/public-list", timeout=3
        )
        if response.status_code == 200:
            zones = response.json()
            if zones:
                closest_zone = None
                min_dist = float("inf")

                for zone in zones:
                    lat_center = zone.get("geofenceLatitude")
                    lng_center = zone.get("geofenceLongitude")
                    if lat_center is not None and lng_center is not None:
                        dist = distance_meters(
                            base_latitude, base_longitude, lat_center, lng_center
                        )
                        if dist < min_dist:
                            min_dist = dist
                            closest_zone = zone

                if closest_zone:
                    print(
                        f"[Sincronizador] Zona más cercana detectada espacialmente: {closest_zone.get('name')} (distancia: {min_dist:.2f}m)"
                    )
                    return closest_zone.get("targetId")

                # Fallback al primer targetId si ninguna zona tiene coordenadas
                return zones[0].get("targetId")
    except Exception as e:
        print(f"[Sincronizador] Advertencia al consultar zones de Spring Boot: {e}")
    return "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def first_present(datos, keys):
    for key in keys:
        if key in datos and datos[key] is not None:
            return datos[key]
    return None


def extraer_lectura_sensor(datos):
    temp = first_present(
        datos,
        [
            "temperatura",
            "temperature",
            "temp",
            "ambientTemperature",
            "ambient_temperature",
            "t",
        ],
    )
    hum = first_present(
        datos,
        ["humedad", "humidity", "hum", "ambientHumidity", "ambient_humidity", "h"],
    )

    if temp is None or hum is None:
        return None

    return float(temp), float(hum)


def guardar_lectura_sensor(temp, hum):
    conn = sqlite3.connect("edge_data.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO lecturas (temperatura, humedad, sincronizado) VALUES (?, ?, 0)",
        (temp, hum),
    )
    conn.commit()
    conn.close()


# ==============================================================================
# 2. TRABAJADOR ASÍNCRONO (Sincronizador con Spring Boot)
# ==============================================================================
def hilo_sincronizador():
    print("[Sincronizador] Trabajador de fondo iniciado y buscando datos...")
    while True:
        try:
            conn = sqlite3.connect("edge_data.db")
            cursor = conn.cursor()

            # CORREGIDO: Cambiado 'humidity' por 'humedad' que es el nombre real en SQLite
            cursor.execute(
                "SELECT id, temperatura, humedad FROM lecturas WHERE sincronizado = 0"
            )
            filas_pendientes = cursor.fetchall()

            # Sincronizar lecturas normales
            if filas_pendientes:
                target_id = obtener_target_id()
                print(
                    f"[Sincronizador] Resolvió dinámicamente targetId={target_id} para la sincronización."
                )

                for fila in filas_pendientes:
                    id_local, temp, hum = fila

                    payload = {
                        "targetId": target_id,
                        "ambientTemperature": temp,
                        "ambientHumidity": hum,
                        "visualData": "none",
                    }

                    print(
                        f"[Sincronizador] Intentando subir registro {id_local} a Spring Boot con formato Swagger..."
                    )
                    response = requests.post(
                        URL_NUBE_TELEMETRIA, json=payload, timeout=3
                    )

                    if response.status_code == 200 or response.status_code == 201:
                        cursor.execute(
                            "UPDATE lecturas SET sincronizado = 1 WHERE id = ?",
                            (id_local,),
                        )
                        conn.commit()
                        print(
                            f"[Sincronizador] ¡Registro {id_local} sincronizado exitosamente en MySQL central!"
                        )
                    else:
                        print(
                            f"[Sincronizador] Spring Boot rechazó el dato. Código: {response.status_code} | Detalle: {response.text}"
                        )

            # Sincronizar ubicaciones
            cursor.execute(
                "SELECT id, latitud, longitud FROM ubicaciones WHERE sincronizado = 0"
            )
            filas_ubicaciones_pendientes = cursor.fetchall()

            if filas_ubicaciones_pendientes:
                target_id = obtener_target_id()
                for fila in filas_ubicaciones_pendientes:
                    id_local, lat, lng = fila

                    payload = {
                        "targetId": target_id,
                        "ambientTemperature": None,
                        "ambientHumidity": None,
                        "visualData": "none",
                        "latitude": lat,
                        "longitude": lng,
                    }

                    print(
                        f"[Sincronizador] Intentando subir ubicación {id_local} a Spring Boot..."
                    )
                    response = requests.post(
                        URL_NUBE_TELEMETRIA, json=payload, timeout=3
                    )

                    if response.status_code == 200 or response.status_code == 201:
                        cursor.execute(
                            "UPDATE ubicaciones SET sincronizado = 1 WHERE id = ?",
                            (id_local,),
                        )
                        conn.commit()
                        print(
                            f"[Sincronizador] ¡Ubicación {id_local} sincronizada exitosamente!"
                        )
                    else:
                        print(
                            f"[Sincronizador] Spring Boot rechazó ubicación. Código: {response.status_code} | Detalle: {response.text}"
                        )

            conn.close()
        except requests.exceptions.ConnectionError:
            print(
                "[Sincronizador] Backend de Spring Boot apagado o inaccesible. Datos seguros en SQLite."
            )
        except Exception as e:
            print(f"[Sincronizador] Error inesperado: {e}")

        time.sleep(10)


# ==============================================================================
# 3. ENDPOINTS DE LA API DEL EDGE GATEWAY (Ajustados a lo que pide tu ESP32)
# ==============================================================================


@app.route("/api/telemetria", methods=["POST"])
@app.route("/telemetria", methods=["POST"])
@app.route("/api/telemetry", methods=["POST"])
@app.route("/telemetry", methods=["POST"])
@app.route("/api/sensor", methods=["POST"])
@app.route("/sensor", methods=["POST"])
def recibir_telemetria():
    try:
        datos = request.get_json() or {}
        print(f"[DEBUG] /telemetria received payload: {datos}")
        lectura = extraer_lectura_sensor(datos)
        if lectura is None:
            return jsonify(
                {
                    "status": "error",
                    "message": "Payload must include temperature and humidity. Accepted keys: temperatura/temperature/temp/ambientTemperature and humedad/humidity/hum/ambientHumidity",
                }
            ), 400

        temp, hum = lectura
        guardar_lectura_sensor(temp, hum)

        print(
            f"[Edge Gateway] ESP32 reportó -> Temp: {temp}°C | Hum: {hum}% (Guardado local)"
        )
        return jsonify(
            {"status": "stored_in_edge", "temperature": temp, "humidity": hum}
        ), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/location", methods=["POST"])
@app.route("/api/location", methods=["POST"])
def recibir_ubicacion():
    global offset_latitude, offset_longitude
    try:
        datos = request.get_json() or {}
        print(f"[DEBUG] /location received payload: {datos}")

        # Override with Edge Gateway simulated state (base coords + offset coords)
        lat_to_save = base_latitude + offset_latitude
        lng_to_save = base_longitude + offset_longitude

        # If simulation is active, increment offsets on each ESP32 GPS tick
        if simulacion_activa:
            offset_latitude += 0.0002
            offset_longitude += 0.0002

        conn = sqlite3.connect("edge_data.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ubicaciones (latitud, longitud, sincronizado) VALUES (?, ?, 0)",
            (lat_to_save, lng_to_save),
        )
        conn.commit()
        conn.close()

        print(
            f"[Edge Gateway] ESP32 GPS tick -> computed Lat: {lat_to_save:.6f} | Lng: {lng_to_save:.6f} (simulacion_activa: {simulacion_activa})"
        )

        lectura = extraer_lectura_sensor(datos)
        if lectura is not None:
            temp, hum = lectura
            guardar_lectura_sensor(temp, hum)
            print(
                f"[Edge Gateway] ESP32 también reportó sensor en GPS tick -> Temp: {temp}°C | Hum: {hum}% (Guardado local)"
            )

        # Evaluar geocerca en tiempo real
        try:
            res_zones = requests.get(
                f"{BACKEND_URL}/api/monitoring/zones/public-list", timeout=2
            )
            if res_zones.status_code == 200:
                zones = res_zones.json()
                for zone in zones:
                    lat_center = zone.get("geofenceLatitude")
                    lng_center = zone.get("geofenceLongitude")
                    radius = zone.get("geofenceRadiusMeters")
                    target_id = zone.get("targetId")

                    if (
                        lat_center is not None
                        and lng_center is not None
                        and radius is not None
                    ):
                        dist = distance_meters(
                            lat_to_save, lng_to_save, lat_center, lng_center
                        )
                        print(
                            f"[Geofence Check] Zone: {zone.get('name')} | Target: {target_id} | Distance: {dist:.2f}m | Radius: {radius:.2f}m"
                        )
                        if dist > radius:
                            print(
                                f"[Geofence Breach] Target {target_id} is OUTSIDE the geofence!"
                            )
                            alert_payload = {
                                "targetId": target_id,
                                "latitude": lat_to_save,
                                "longitude": lng_to_save,
                            }
                            res_alert = requests.post(
                                f"{BACKEND_URL}/api/monitoring/alerts/evaluate",
                                json=alert_payload,
                                timeout=2,
                            )
                            if res_alert.status_code in [200, 201]:
                                print(
                                    f"[Geofence Alert] Breach alert created/updated on Spring Boot."
                                )
                            else:
                                print(
                                    f"[Geofence Alert] Cloud returned code {res_alert.status_code} for breach evaluation."
                                )
        except Exception as ge_err:
            print(f"[Geofence Check] Error during live evaluation: {ge_err}")

        response_payload = {
            "status": "stored_in_edge",
            "latitude": lat_to_save,
            "longitude": lng_to_save,
        }
        if lectura is not None:
            response_payload["temperature"] = temp
            response_payload["humidity"] = hum
        return jsonify(response_payload), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/simulador/config", methods=["POST"])
def configurar_simulador():
    global \
        base_latitude, \
        base_longitude, \
        offset_latitude, \
        offset_longitude, \
        active_target_id
    try:
        datos = request.get_json() or {}
        base_latitude = datos.get("latitude", -12.046374)
        base_longitude = datos.get("longitude", -77.042793)
        offset_latitude = 0.0
        offset_longitude = 0.0
        active_target_id = datos.get("targetId")
        print(
            f"[Simulador] Base configurada -> Lat: {base_latitude}, Lng: {base_longitude} | TargetID: {active_target_id}"
        )
        return jsonify(
            {
                "status": "configured",
                "latitude": base_latitude,
                "longitude": base_longitude,
                "targetId": active_target_id,
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/simulador/iniciar", methods=["POST"])
def iniciar_simulacion():
    global simulacion_activa
    simulacion_activa = True
    print("[Simulador] Simulación de movimiento ACTIVADA.")
    return jsonify({"status": "started", "simulacion_activa": simulacion_activa}), 200


@app.route("/api/simulador/detener", methods=["POST"])
def detener_simulacion():
    global \
        simulacion_activa, \
        base_latitude, \
        base_longitude, \
        offset_latitude, \
        offset_longitude, \
        active_target_id
    simulacion_activa = False
    offset_latitude = 0.0
    offset_longitude = 0.0
    base_latitude = -12.046374
    base_longitude = -77.042793
    active_target_id = None
    print(
        "[Simulador] Simulación de movimiento DETENIDA. Coordenadas y TargetID restablecidas."
    )

    # Eliminar ubicaciones locales en caché
    try:
        conn = sqlite3.connect("edge_data.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ubicaciones")
        conn.commit()
        conn.close()
        print("[Simulador] Historial de ubicaciones locales eliminado.")
    except Exception as e:
        print(f"[Simulador] Error al limpiar SQLite: {e}")

    return jsonify(
        {
            "status": "stopped",
            "simulacion_activa": simulacion_activa,
            "latitude": base_latitude,
            "longitude": base_longitude,
        }
    ), 200


@app.route("/api/simulador/regresar", methods=["POST"])
def regresar_simulacion():
    global \
        base_latitude, \
        base_longitude, \
        offset_latitude, \
        offset_longitude, \
        active_target_id
    offset_latitude = 0.0
    offset_longitude = 0.0
    base_latitude = -12.046374
    base_longitude = -77.042793
    active_target_id = None
    print("[Simulador] Coordenadas y TargetID regresadas a posición inicial.")
    return jsonify(
        {"status": "reset", "latitude": base_latitude, "longitude": base_longitude}
    ), 200


@app.route("/api/simulador/alejar", methods=["POST"])
def alejar_simulacion():
    global offset_latitude, offset_longitude
    offset_latitude = 0.045
    offset_longitude = 0.045
    print("[Simulador] Ubicación alejada 5km para probar alertas.")
    return jsonify(
        {
            "status": "relocated_far",
            "latitude": base_latitude + offset_latitude,
            "longitude": base_longitude + offset_longitude,
        }
    ), 200


@app.route("/api/simulador/estado", methods=["GET"])
def obtener_estado_simulador():
    return jsonify(
        {
            "simulacion_activa": simulacion_activa,
            "latitude": base_latitude + offset_latitude,
            "longitude": base_longitude + offset_longitude,
        }
    ), 200


@app.route("/api/dispensador/status", methods=["GET"])
def check_dispenser_status():
    return jsonify({"activar": alimentar_pendiente}), 200


@app.route("/api/dispensador/confirmar", methods=["POST"])
def confirmar_entrega_alimento():
    global alimentar_pendiente
    alimentar_pendiente = False
    print("[Edge Gateway] El ESP32 confirmó la acción del servo. Cola limpia.")
    return jsonify({"status": "acknowledged"}), 200


@app.route("/api/dispensador/forzar_alimento", methods=["POST"])
def forzar_orden_alimento():
    global alimentar_pendiente
    alimentar_pendiente = True
    print("[Simulador] Orden manual inyectada. Esperando al ESP32...")
    return jsonify({"status": "queued"}), 200


@app.route("/api/dispensador/configurar_horario", methods=["POST"])
def configurar_horario():
    global horario_activo, horario_intervalo_segundos, ultimo_envio_horario
    try:
        datos = request.get_json() or {}
        activo = datos.get("activo", False)
        intervalo_str = datos.get("intervalo", "cada 1 minuto")

        horario_activo = activo
        horario_intervalo_segundos = parse_intervalo_segundos(intervalo_str)

        if horario_activo:
            ultimo_envio_horario = time.time()

        estado_str = "ACTIVADO" if horario_activo else "DESACTIVADO"
        print(
            f"[Edge Gateway] Modo horario {estado_str}. Intervalo: {intervalo_str} ({horario_intervalo_segundos}s)"
        )

        return jsonify(
            {
                "activo": horario_activo,
                "intervalo_segundos": horario_intervalo_segundos,
                "status": "configured",
            }
        ), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/dispensador/configurar_horario", methods=["GET"])
def obtener_configuracion_horario():
    return jsonify(
        {"activo": horario_activo, "intervalo_segundos": horario_intervalo_segundos}
    ), 200


def hilo_dispensador_horario():
    global \
        alimentar_pendiente, \
        horario_activo, \
        horario_intervalo_segundos, \
        ultimo_envio_horario
    print("[Edge Gateway] Hilo de dispensación automática por horario iniciado.")
    while True:
        try:
            if horario_activo:
                ahora = time.time()
                if ahora - ultimo_envio_horario >= horario_intervalo_segundos:
                    alimentar_pendiente = True
                    ultimo_envio_horario = ahora
                    print(
                        f"[Edge Gateway] ¡Horario disparado! Activando dispensador automáticamente. Siguiente en {horario_intervalo_segundos}s."
                    )
        except Exception as e:
            print(f"[Edge Gateway] Error en hilo de dispensación por horario: {e}")
        time.sleep(1)


if __name__ == "__main__":
    init_database()
    worker = threading.Thread(target=hilo_sincronizador, daemon=True)
    worker.start()

    worker_horario = threading.Thread(target=hilo_dispensador_horario, daemon=True)
    worker_horario.start()

    # Puerto 18090 para evitar conflictos con restricciones de Windows
    app.run(host="0.0.0.0", port=18090, debug=False)
