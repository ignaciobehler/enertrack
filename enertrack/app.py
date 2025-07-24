"""app.py – EnerTrack

Back‑end Flask para gestión de usuarios y nodos + KPIs globales.
Compatible con MariaDB (Flask‑MySQLdb) e InfluxDB v2.  Si la librería
influxdb-client no está disponible o las variables de entorno no están
completas, el sistema devuelve datos ficticios para que la UI funcione
igualmente.
"""

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, current_app
)
from flask_mysqldb import MySQL
import os, logging
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import MySQLdb.cursors
from datetime import datetime
import threading
import time
import ssl
import asyncio
import re
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import requests as pyrequests

# ───────────────────────────── Logging ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - ENERGY - %(levelname)s - %(message)s",
)
logger = logging.getLogger()

# ────────────────────── InfluxDB (opcional) ───────────────────────
try:
    from influxdb_client import InfluxDBClient, Point  # type: ignore
except ModuleNotFoundError:
    InfluxDBClient = None  # type: ignore
    logger.warning("InfluxDB client not installed; usando KPIs ficticios")


def build_influx_client():
    """Devuelve un cliente InfluxDB o None si falta config."""
    if not InfluxDBClient:
        return None
    url   = os.getenv("INFLUX_URL")
    token = os.getenv("INFLUX_TOKEN")
    org   = os.getenv("INFLUX_ORG")
    if not all([url, token, org]):
        logger.warning("Variables de entorno Influx incompletas → datos ficticios")
        return None
    return InfluxDBClient(url=url, token=token, org=org)

influx_client = build_influx_client()
influx_bucket  = os.getenv("INFLUX_BUCKET", "")

# Configuración MQTT desde variables de entorno
MQTT_DOMINIO = os.getenv("DOMINIO")
MQTT_PORT = int(os.getenv("PUERTO_MQTTS", "8883"))
MQTT_USER = os.getenv("MQTT_USR")
MQTT_PASS = os.getenv("MQTT_PASS")

# Verificar configuración MQTT
if not all([MQTT_DOMINIO, MQTT_USER, MQTT_PASS]):
    logger.warning("⚠️ Variables de entorno MQTT incompletas:")
    logger.warning(f"  DOMINIO: {MQTT_DOMINIO}")
    logger.warning(f"  MQTT_USR: {MQTT_USER}")
    logger.warning(f"  MQTT_PASS: {'***' if MQTT_PASS else 'No configurada'}")
    logger.warning(f"  PUERTO_MQTTS: {MQTT_PORT}")
else:
    logger.info("✅ Configuración MQTT completa")

# TLS context para MQTTS
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED

# Magnitudes a escuchar
MAGNITUDES = ["tension", "corriente", "consumo", "fp", "frecuencia"]

# ───────────────────────── Flask & MySQL ─────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configurar aplicación para funcionar bajo el prefijo /enertrack
app.config['APPLICATION_ROOT'] = '/enertrack'

# Inyectar el año actual en todas las plantillas
from datetime import datetime
@app.context_processor
def inject_now():
    return {'current_year': datetime.now().year}

@app.before_request
def before_request():
    if request.script_root != app.config['APPLICATION_ROOT']:
        request.environ['SCRIPT_NAME'] = app.config['APPLICATION_ROOT']

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")
app.config.update(
    MYSQL_USER     = os.getenv("MYSQL_USER", "root"),
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", ""),
    MYSQL_DB       = os.getenv("MYSQL_DB", "medidoresEnergia"),
    MYSQL_HOST     = os.getenv("MYSQL_HOST", "localhost"),
    MYSQL_CURSORCLASS = "DictCursor",  # devuelve dicts por defecto
    PERMANENT_SESSION_LIFETIME = 180,
)
mysql = MySQL(app)
logger.info(
    "Conectado a MariaDB %s → BD %s como %s",
    app.config["MYSQL_HOST"], app.config["MYSQL_DB"], app.config["MYSQL_USER"]
)

# ───────────────────── Decorador de autenticación ─────────────────

def require_login(fn):
    @wraps(fn)
    def _wrap(*a, **kw):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return _wrap

# ───────────────────────── Función KPI global ─────────────────────

def get_global_kpis(esp_ids: list[str]):
    """Devuelve un dict con promedios globales de los últimos 30 minutos y estado de nodos. Si Influx no está, dummy."""
    if not esp_ids:
        return {"volt": "-", "curr": "-", "ener": "-", "pf": "-", "freq": "-", "sin_datos": True, "nodos_estado": {}}

    if influx_client and influx_bucket:
        esp_filter = " or ".join([f'r["esp_id"] == "{esp_id}"' for esp_id in esp_ids])
        # --- A) POTENCIA MEDIA GLOBAL (energía total de consumo) ---
        flux_consumo = f'''
            from(bucket: "{influx_bucket}")
              |> range(start: -30m)
              |> filter(fn: (r) => {esp_filter})
              |> filter(fn: (r) => r._measurement == "consumo" and r._field == "valor")
              |> sum()
        '''
        # --- B) MEDIA DE MAGNITUDES INSTANTÁNEAS ---
        flux_inst = f'''
            from(bucket: "{influx_bucket}")
              |> range(start: -30m)
              |> filter(fn: (r) => {esp_filter})
              |> filter(fn: (r) => r._measurement != "consumo" and r._field == "valor")
              |> group(columns: ["_measurement"])
              |> mean()
        '''
        try:
            # --- consumo total y potencia media global ---
            energia_total_kwh = 0.0  # comienza en cero y acumula
            for table in influx_client.query_api().query(flux_consumo):
                for rec in table.records:
                    v = rec.get_value()
                    if v is not None:
                        energia_total_kwh += float(v)   # suma cada nodo

            potencia_media_kw = (energia_total_kwh / 0.5) if energia_total_kwh else None  # 30 min = 0,5 h
            # --- medias instantáneas ---
            kpi_inst = {"tension": "-", "corriente": "-", "fp": "-", "frecuencia": "-"}
            for t in influx_client.query_api().query(flux_inst):
                for rec in t.records:
                    m = rec.values.get("_measurement")
                    kpi_inst[m] = f"{rec.get_value():.2f}"
            # --- empaquetar resultado ---
            kpis_formateados = {
                "volt": kpi_inst["tension"],
                "curr": kpi_inst["corriente"],
                "pf":   kpi_inst["fp"],
                "freq": kpi_inst["frecuencia"],
                "ener": f"{energia_total_kwh:.2f}" if energia_total_kwh else "-",
                "potencia_media_global_kw": f"{potencia_media_kw:.2f}" if potencia_media_kw else "-",
                "sin_datos": energia_total_kwh is None,
            }
            # Determinar estado de cada nodo
            nodos_estado = {}
            for esp_id in esp_ids:
                estado = get_nodo_estado(esp_id)
                nodos_estado[esp_id] = estado
            kpis_formateados["nodos_estado"] = nodos_estado
            return kpis_formateados
        except Exception as e:
            logger.error("Error Influx: %s", e)
    return {"volt": "-", "curr": "-", "ener": "-", "pf": "-", "freq": "-", "sin_datos": True, "nodos_estado": {}}


def get_nodo_estado(esp_id: str):
    """
    Determina el estado de un nodo basado en si tiene datos recientes.
    La última actualización será la fecha más reciente de cualquier magnitud.
    """
    if not influx_client or not influx_bucket:
        return {"estado": "desconocido", "ultima_actualizacion": None, "magnitudes_activas": 0}
    
    try:
        # Verificar si hay datos recientes (últimos 30 minutos)
        flux_reciente = f'''
            from(bucket: "{influx_bucket}")
              |> range(start: -30m)
              |> filter(fn: (r) => r["esp_id"] == "{esp_id}")
              |> filter(fn: (r) => r["_measurement"] == "tension" or r["_measurement"] == "corriente" or r["_measurement"] == "consumo" or r["_measurement"] == "fp" or r["_measurement"] == "frecuencia")
              |> filter(fn: (r) => r["_field"] == "valor")
              |> count()
        '''
        
        # Obtener el timestamp más reciente de cualquier magnitud
        flux_ultimo_general = f'''
            from(bucket: "{influx_bucket}")
              |> range(start: -24h)
              |> filter(fn: (r) => r["esp_id"] == "{esp_id}")
              |> filter(fn: (r) => r["_measurement"] == "tension" or r["_measurement"] == "corriente" or r["_measurement"] == "consumo" or r["_measurement"] == "fp" or r["_measurement"] == "frecuencia")
              |> filter(fn: (r) => r["_field"] == "valor")
              |> keep(columns: ["_time"])
              |> sort(columns: ["_time"], desc: true)
              |> limit(n:1)
        '''
        
        # Contar datos recientes
        tables_recientes = list(influx_client.query_api().query(flux_reciente))
        datos_recientes = 0
        for table in tables_recientes:
            for record in table.records:
                datos_recientes = record.get_value()
        
        logger.info(f"🔍 Datos recientes para {esp_id}: {datos_recientes}")
        logger.info(f"🔍 Query datos recientes: {flux_reciente}")
        
        # Obtener la última actualización real (de cualquier magnitud)
        tables_ultimo = list(influx_client.query_api().query(flux_ultimo_general))
        ultima_actualizacion = None
        if tables_ultimo:
            for table in tables_ultimo:
                for record in table.records:
                    utc_time = record.get_time()
                    from datetime import timedelta
                    ultima_actualizacion = utc_time - timedelta(hours=3)
                    break
                if ultima_actualizacion:
                    break
        
        # Determinar estado basado en datos recientes
        if datos_recientes > 0:
            estado = "activo"
            logger.info(f"✅ Nodo {esp_id} ACTIVO: {datos_recientes} datos recientes")
        elif ultima_actualizacion:
            # Verificar si la última actualización fue hace más de 30 minutos
            from datetime import datetime, timedelta
            ahora = datetime.now()
            tiempo_transcurrido = ahora - ultima_actualizacion.replace(tzinfo=None)
            if tiempo_transcurrido <= timedelta(minutes=30):
                estado = "activo"
                logger.info(f"✅ Nodo {esp_id} ACTIVO: última actualización hace {tiempo_transcurrido.total_seconds()/60:.1f} minutos")
            else:
                estado = "desconectado"
                logger.info(f"❌ Nodo {esp_id} DESCONECTADO: última actualización hace {tiempo_transcurrido.total_seconds()/60:.1f} minutos")
        else:
            estado = "sin_datos"
            ultima_actualizacion = None
            logger.info(f"⚠️ Nodo {esp_id} SIN DATOS: no se encontraron datos históricos")
        
        return {
            "estado": estado,
            "ultima_actualizacion": ultima_actualizacion.isoformat() if ultima_actualizacion else None,
            "magnitudes_activas": datos_recientes
        }
        
    except Exception as e:
        logger.error(f"Error determinando estado del nodo {esp_id}: {e}")
        return {"estado": "error", "ultima_actualizacion": None, "magnitudes_activas": 0}

# ───────────────────────────── Rutas Auth ─────────────────────────

@app.route("/registrar", methods=["GET", "POST"])
def registrar():
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Usuario y contraseña obligatorios", "warning")
            return redirect(url_for("registrar"))

        phash = generate_password_hash(password, method="scrypt", salt_length=16)
        cur = mysql.connection.cursor()
        try:
            cur.execute(
                "INSERT INTO Usuarios (nombreUsuario, password_hash) VALUES (%s,%s)",
                (username, phash),
            )
            mysql.connection.commit()
            flash("Usuario creado, inicia sesión", "success")
            return redirect(url_for("login"))
        except Exception as e:
            mysql.connection.rollback()
            logger.error(e)
            flash("El usuario ya existe", "danger")
            return redirect(url_for("registrar"))

    return render_template("registrar.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")
        cur = mysql.connection.cursor()
        cur.execute("SELECT usuario_id, password_hash FROM Usuarios WHERE nombreUsuario=%s", (username,))
        row = cur.fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.permanent = True
            session["user_id"]  = row["usuario_id"]
            session["username"] = username
            
            # Seleccionar automáticamente el primer nodo y lanzar worker
            cur2 = mysql.connection.cursor()
            cur2.execute("""
                SELECT N.esp_id FROM Nodos N 
                JOIN UsuariosNodos UN USING(nodo_id) 
                WHERE UN.usuario_id=%s ORDER BY N.nodo_id ASC LIMIT 1
            """, (session["user_id"],))
            primer_nodo = cur2.fetchone()
            if primer_nodo:
                session["esp_id_seleccionado"] = primer_nodo["esp_id"]
                # start_device_worker(primer_nodo["esp_id"]) # Eliminado
            cur2.close()
            
            return redirect(url_for("index"))
        flash("Credenciales inválidas", "danger")
    return render_template("login.html")


@app.route("/cerrar-sesion")
@require_login
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ───────────────────────────── Rutas App ──────────────────────────

@app.route("/", endpoint="index")
@require_login
def home():
    uid = session["user_id"]
    logger.info(f"🏠 Página principal - Usuario ID: {uid}, Username: {session.get('username')}")
    
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion, UN.activo, UN.ultimo_acceso
               FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id)
               WHERE UN.usuario_id=%s""",
        (uid,),
    )
    nodos = cur.fetchall()
    logger.info(f"📋 Nodos encontrados para usuario {uid}: {len(nodos)} nodos")
    for nodo in nodos:
        logger.info(f"  📍 Nodo: ID={nodo['nodo_id']}, ESP={nodo['esp_id']}, Desc={nodo['descripcion']}")
    
    kpis = get_global_kpis([n["esp_id"] for n in nodos])

    # Calcular contadores de estados de nodos
    activos = 0
    desconectados = 0
    sin_datos = 0
    for n in nodos:
        nodo_estado = kpis["nodos_estado"].get(n["esp_id"], {})
        if nodo_estado.get("estado") == "activo":
            activos += 1
        elif nodo_estado.get("estado") == "desconectado":
            desconectados += 1
        elif nodo_estado.get("estado") == "sin_datos":
            sin_datos += 1
        else:
            sin_datos += 1

    return render_template("home.html", nodos=nodos, kpis=kpis, activos=activos, desconectados=desconectados, sin_datos=sin_datos)


@app.route("/add_node", methods=["POST"])
@require_login
def add_node():
    uid        = session["user_id"]
    esp_id     = request.form.get("esp_id", "").strip()
    descripcion= request.form.get("descripcion", "").strip()
    ubicacion  = request.form.get("ubicacion", "").strip()

    if not esp_id or not descripcion:
        flash("ESP‑ID y descripción son obligatorios", "warning")
        return redirect(url_for("mis_nodos"))

    cur = mysql.connection.cursor()
    try:
        # Verificar si el usuario actual ya tiene un nodo con ese ESP ID
        cur.execute("""
            SELECT 1 FROM Nodos N 
            JOIN UsuariosNodos UN USING(nodo_id) 
            WHERE UN.usuario_id=%s AND N.esp_id=%s
        """, (uid, esp_id))
        
        if cur.fetchone():
            flash("Ya tienes un nodo registrado con ese ESP ID", "danger")
            return redirect(url_for("mis_nodos"))
        
        # Buscar si el nodo ya existe en la base de datos (puede estar registrado por otro usuario)
        cur.execute("SELECT nodo_id FROM Nodos WHERE esp_id=%s", (esp_id,))
        row = cur.fetchone()
        
        if row:
            # El nodo ya existe, solo vincularlo al usuario actual
            nodo_id = row["nodo_id"]
            # Actualizar la descripción si es diferente
            cur.execute("UPDATE Nodos SET descripcion=%s WHERE nodo_id=%s", (descripcion, nodo_id))
        else:
            # El nodo no existe, crearlo
            cur.execute(
                "INSERT INTO Nodos (esp_id, descripcion) VALUES (%s,%s)",
                (esp_id, descripcion),
            )
            nodo_id = cur.lastrowid
        # Vincular al usuario (sin umbral_consumo)
        cur.execute(
            """INSERT IGNORE INTO UsuariosNodos (usuario_id, nodo_id, ubicacion)
                    VALUES (%s,%s,%s)""",
            (uid, nodo_id, ubicacion or None),
        )
        mysql.connection.commit()
        flash("Nodo agregado", "success")
    except Exception as e:
        mysql.connection.rollback()
        logger.error(e)
        flash("Error al agregar nodo", "danger")
    return redirect(url_for("mis_nodos"))


# ───────────────────────────── API JSON ──────────────────────────

@app.route("/nodo/<int:nodo_id>")
@require_login
def nodo_detalle(nodo_id):
    """Redirigir al dashboard overview moderno del nodo."""
    return redirect(url_for("nodo_dashboard", nodo_id=nodo_id))


@app.route("/nodo/<int:nodo_id>/dashboard")
@require_login
def nodo_dashboard(nodo_id):
    """Dashboard overview de un nodo con tarjetas KPI para cada magnitud."""
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion, UN.activo, UN.ultimo_acceso
               FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id)
               WHERE UN.usuario_id=%s AND N.nodo_id=%s""",
        (uid, nodo_id),
    )
    nodo = cur.fetchone()
    if not nodo:
        flash("Nodo no encontrado o sin acceso", "danger")
        return redirect(url_for("index"))
    return render_template("dashboards.html", nodo=nodo)


@app.route("/nodo/<int:nodo_id>/dashboard/<magnitud>")
@require_login
def nodo_dashboard_magnitud(nodo_id, magnitud):
    """Dashboard individual de una magnitud para un nodo."""
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion, UN.activo, UN.ultimo_acceso
               FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id)
               WHERE UN.usuario_id=%s AND N.nodo_id=%s""",
        (uid, nodo_id),
    )
    nodo = cur.fetchone()
    if not nodo:
        flash("Nodo no encontrado o sin acceso", "danger")
        return redirect(url_for("index"))
    return render_template("nodo_dashboard_magnitud.html", nodo=nodo, magnitud=magnitud)





@app.route("/mis-nodos")
@require_login
def mis_nodos():
    """Página de gestión de nodos del usuario."""
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion, UN.activo, UN.ultimo_acceso
               FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id)
               WHERE UN.usuario_id=%s
               ORDER BY N.esp_id""",
        (uid,),
    )
    nodos = cur.fetchall()
    kpis = get_global_kpis([n["esp_id"] for n in nodos])
    # Obtener si el usuario tiene Telegram vinculado
    cur.execute("SELECT telegram_chat_id FROM Usuarios WHERE usuario_id=%s", (uid,))
    row = cur.fetchone()
    telegram_vinculado = bool(row and row['telegram_chat_id'])
    return render_template("mis_nodos.html", nodos=nodos, kpis=kpis, telegram_vinculado=telegram_vinculado)


@app.route("/consumo")
@require_login
def consumo_global():
    """Página de análisis de consumo global de todos los nodos del usuario."""
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion, UN.activo, UN.ultimo_acceso
               FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id)
               WHERE UN.usuario_id=%s
               ORDER BY N.esp_id""",
        (uid,),
    )
    nodos = cur.fetchall()
    return render_template("consumo_global.html", nodos=nodos)


@app.route("/nodo/<int:nodo_id>/remove", methods=["POST"])
@require_login
def remove_node(nodo_id):
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    try:
        cur.execute(
            "SELECT esp_id FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s AND N.nodo_id=%s",
            (uid, nodo_id),
        )
        nodo = cur.fetchone()
        if not nodo:
            flash("Nodo no encontrado o sin acceso", "danger")
            return redirect(url_for("mis_nodos"))
        # Eliminar la relación usuario-nodo
        cur.execute(
            "DELETE FROM UsuariosNodos WHERE usuario_id=%s AND nodo_id=%s",
            (uid, nodo_id),
        )
        # Si el nodo ya no está vinculado a ningún usuario, eliminarlo completamente
        cur.execute("SELECT 1 FROM UsuariosNodos WHERE nodo_id=%s", (nodo_id,))
        if not cur.fetchone():
            cur.execute("DELETE FROM Nodos WHERE nodo_id=%s", (nodo_id,))
        mysql.connection.commit()
        flash(f"Nodo {nodo['esp_id']} eliminado de tu lista", "success")
    except Exception as e:
        mysql.connection.rollback()
        logger.error(e)
        flash("Error al eliminar el nodo", "danger")
    return redirect(url_for("mis_nodos"))


@app.route("/nodo/<int:nodo_id>/update", methods=["GET", "POST"])
@require_login
def update_node(nodo_id):
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute(
        """SELECT N.nodo_id, N.esp_id, N.descripcion, UN.ubicacion FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s AND N.nodo_id=%s""",
        (uid, nodo_id),
    )
    nodo = cur.fetchone()
    if not nodo:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
            return jsonify({'error': 'Nodo no encontrado o sin acceso'}), 404
        flash("Nodo no encontrado o sin acceso", "danger")
        return redirect(url_for("mis_nodos"))
    if request.method == "POST":
        nuevo_esp_id = request.form.get("esp_id", "").strip()
        descripcion = request.form.get("descripcion", "").strip()
        ubicacion = request.form.get("ubicacion", "").strip()
        if not nuevo_esp_id or not descripcion:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                return jsonify({'error': 'ESP ID y descripción son obligatorios'}), 400
            flash("ESP ID y descripción son obligatorios", "warning")
            return redirect(url_for("mis_nodos"))
        try:
            cur.execute("""
                SELECT 1 FROM Nodos N 
                JOIN UsuariosNodos UN USING(nodo_id) 
                WHERE UN.usuario_id=%s AND N.esp_id=%s AND N.nodo_id!=%s
            """, (uid, nuevo_esp_id, nodo_id))
            if cur.fetchone():
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                    return jsonify({'error': 'Ya tienes otro nodo registrado con ese ESP ID'}), 400
                flash("Ya tienes otro nodo registrado con ese ESP ID", "danger")
                return redirect(url_for("mis_nodos"))
            cur.execute("UPDATE Nodos SET esp_id=%s, descripcion=%s WHERE nodo_id=%s", (nuevo_esp_id, descripcion, nodo_id))
            cur.execute("UPDATE UsuariosNodos SET ubicacion=%s WHERE usuario_id=%s AND nodo_id=%s", (ubicacion or None, uid, nodo_id))
            mysql.connection.commit()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                return jsonify({'success': True}), 200
            flash("Nodo actualizado correctamente", "success")
        except Exception as e:
            mysql.connection.rollback()
            logger.error(e)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                return jsonify({'error': 'Error al actualizar nodo'}), 500
            flash("Error al actualizar nodo", "danger")
        return redirect(url_for("mis_nodos"))
    return redirect(url_for("mis_nodos"))


@app.route("/api/nodo/<int:nodo_id>/latest")
@require_login
def api_nodo_latest(nodo_id):
    logger.info(f"[DASHBOARDS] INICIO llamada a /api/nodo/{nodo_id}/latest para usuario {session.get('user_id')}")
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    # Verificar acceso al nodo y obtener datos de configuración
    cur.execute(
        "SELECT N.esp_id, N.descripcion, UN.ubicacion FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s AND N.nodo_id=%s",
        (uid, nodo_id),
    )
    nodo = cur.fetchone()
    if not nodo:
        logger.error(f"[DASHBOARDS] Nodo {nodo_id} no encontrado para usuario {uid}")
        return jsonify({"error": "Nodo no encontrado"}), 404
    if not influx_client or not influx_bucket:
        logger.error(f"[DASHBOARDS] InfluxDB no disponible para usuario {uid}, nodo {nodo_id}")
        return jsonify({"error": "InfluxDB no disponible"}), 503
    esp_id = nodo["esp_id"]
    magnitudes = ["tension", "corriente", "consumo", "fp", "frecuencia"]
    result = {"esp_id": esp_id, "descripcion": nodo["descripcion"], "ubicacion": nodo["ubicacion"]}
    try:
        from datetime import timedelta
        for mag in magnitudes:
            logger.info(f"[DASHBOARDS] Consultando magnitud '{mag}' para nodo {esp_id} (usuario {uid}) en los últimos 30 minutos...")
            flux = f'''
                from(bucket: "{influx_bucket}")
                  |> range(start: -30m)
                  |> filter(fn: (r) => r["_measurement"] == "{mag}")
                  |> filter(fn: (r) => r["esp_id"] == "{esp_id}")
                  |> filter(fn: (r) => r["_field"] == "valor")
                  |> sort(columns: ["_time"])
            '''
            logger.info(f"[DASHBOARDS] Query Flux 30m para {mag}: {flux}")
            tables = list(influx_client.query_api().query(flux))
            valores = []
            tiempos = []
            for table in tables:
                for record in table.records:
                    valor = record.get_value()
                    utc_time = record.get_time()
                    local_time = utc_time - timedelta(hours=3)
                    valores.append(valor)
                    tiempos.append(local_time)
            logger.info(f"[DASHBOARDS] Magnitud '{mag}': {len(valores)} valores en 30m: {valores}")
            # Si no hay datos en la última media hora, buscar el último dato en las últimas 24h
            if not valores:
                logger.info(f"[DASHBOARDS] Sin datos en 30m para '{mag}', buscando en 24h...")
                flux_24h = f'''
                    from(bucket: "{influx_bucket}")
                      |> range(start: -24h)
                      |> filter(fn: (r) => r["_measurement"] == "{mag}")
                      |> filter(fn: (r) => r["esp_id"] == "{esp_id}")
                      |> filter(fn: (r) => r["_field"] == "valor")
                      |> sort(columns: ["_time"])
                '''
                logger.info(f"[DASHBOARDS] Query Flux 24h para {mag}: {flux_24h}")
                tables_24h = list(influx_client.query_api().query(flux_24h))
                for table in tables_24h:
                    for record in table.records:
                        valor = record.get_value()
                        utc_time = record.get_time()
                        local_time = utc_time - timedelta(hours=3)
                        valores.append(valor)
                        tiempos.append(local_time)
                logger.info(f"[DASHBOARDS] Magnitud '{mag}': {len(valores)} valores en 24h: {valores}")
            if valores:
                actual = valores[-1]
                timestamp = tiempos[-1].isoformat()
                maximo = max(valores)
                minimo = min(valores)
                logger.info(f"[DASHBOARDS] Magnitud '{mag}' RESULTADO: actual={actual}, max={maximo}, min={minimo}, timestamp={timestamp}")
                result[mag] = {
                    "actual": actual,
                    "max": maximo,
                    "min": minimo,
                    "timestamp": timestamp
                }
            else:
                logger.warning(f"[DASHBOARDS] Magnitud '{mag}' SIN DATOS para nodo {esp_id}")
                result[mag] = {
                    "actual": None,
                    "max": None,
                    "min": None,
                    "timestamp": None
                }
        # Añadir estado y última actualización
        estado_info = get_nodo_estado(esp_id)
        result["estado"] = estado_info["estado"]
        result["ultima_actualizacion"] = estado_info["ultima_actualizacion"]
        logger.info(f"[DASHBOARDS] FIN llamada a /api/nodo/{nodo_id}/latest → resultado: {result}")
        return jsonify(result)
    except Exception as e:
        logger.error(f"[DASHBOARDS] Error consultando InfluxDB para nodo {esp_id}: {e}")
        return jsonify({"error": "Error consultando InfluxDB"}), 500

@app.route("/api/nodo/<int:nodo_id>/magnitud/<magnitud>")
@require_login
def api_nodo_magnitud(nodo_id, magnitud):
    """API para obtener datos de una magnitud específica de un nodo, filtrando por el rango de tiempo solicitado."""
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    # Verificar acceso al nodo
    cur.execute(
        "SELECT N.esp_id FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s AND N.nodo_id=%s",
        (uid, nodo_id),
    )
    nodo = cur.fetchone()
    if not nodo:
        return jsonify({"error": "Nodo no encontrado"}), 404
    if not influx_client or not influx_bucket:
        return jsonify({"error": "InfluxDB no disponible"}), 503
    esp_id = nodo["esp_id"]
    # Obtener parámetro de rango (en minutos, por defecto 60)
    try:
        rango_min = int(request.args.get('rango', '60'))
    except Exception:
        rango_min = 60
    rango_str = f"-{rango_min}m"
    
    logger.info(f"🔍 Consultando InfluxDB: nodo_id={nodo_id}, esp_id={esp_id}, magnitud={magnitud}, rango={rango_str}")
    logger.info(f"🔧 Configuración InfluxDB: bucket={influx_bucket}, client={influx_client is not None}")
    
    # Verificar si hay datos en el rango
    check_query = f'''
        from(bucket: "{influx_bucket}")
          |> range(start: {rango_str})
          |> filter(fn: (r) => r._measurement == "{magnitud}" and r.esp_id == "{esp_id}")
          |> count()
    '''
    try:
        check_result = influx_client.query_api().query(check_query)
        count = 0
        for table in check_result:
            for record in table.records:
                count = record.get_value()
        logger.info(f"📊 Total de registros en rango: {count}")
    except Exception as e:
        logger.error(f"❌ Error verificando conteo: {e}")
    
    try:
        # Consulta simple como en el código de referencia
        flux = f'''
            from(bucket: "{influx_bucket}")
              |> range(start: {rango_str})
              |> filter(fn: (r) => r._measurement == "{magnitud}" and r.esp_id == "{esp_id}")
              |> filter(fn: (r) => r._field == "valor")
              |> group()
              |> sort(columns: ["_time"])
        '''
        logger.info(f"📊 Query Flux: {flux}")
        
        result = influx_client.query_api().query(flux)
        datos = []
        seen_times = set()  # Para evitar duplicados
        
        logger.info(f"🔍 === CONSULTA api_nodo_magnitud ===")
        logger.info(f"📊 Nodo: {esp_id}, Magnitud: {magnitud}, Rango: {rango_str}")
        logger.info(f"📊 Query: {flux}")
        
        for table in result:
            logger.info(f"📊 Tabla api_nodo_magnitud: {len(table.records)} registros")
            for record in table.records:
                # Convertir UTC a hora local (UTC-3 para Argentina)
                utc_time = record.get_time()
                # Convertir a hora local de Argentina (UTC-3)
                from datetime import timedelta
                local_time = utc_time - timedelta(hours=3)
                valor = record.get_value()
                
                logger.info(f"  📝 api_nodo_magnitud: {local_time} = {valor}")
                
                # Formatear la hora para mostrar solo HH:MM:SS
                time_key = local_time.strftime('%H:%M:%S')
                if time_key not in seen_times:
                    seen_times.add(time_key)
                    datos.append({
                        "fecha": local_time.strftime('%d/%m/%Y'),
                        "hora": local_time.strftime('%H:%M:%S'),
                        "tiempo": local_time.strftime('%H:%M:%S'),  # Para compatibilidad con frontend actual
                        "valor": valor
                    })
                    logger.info(f"    ✅ Agregado: {time_key} = {valor}")
                else:
                    logger.info(f"    ❌ Duplicado descartado: {time_key} = {valor}")
        
        logger.info(f"🔍 === FIN CONSULTA api_nodo_magnitud ===")
        
        # Calcular estadísticas como en el código de referencia
        valores = [d["valor"] for d in datos]
        total = sum(valores) if valores else None
        estadisticas = {
            "actual": valores[-1] if valores else None,
            "maximo": max(valores) if valores else None,
            "minimo": min(valores) if valores else None,
            "total": total
        }
        # Solo para consumo: potencia_media_kw
        if magnitud == "consumo" and valores:
            try:
                rango_min = int(request.args.get('rango', '60'))
            except Exception:
                rango_min = 60
            duracion_horas = rango_min / 60
            potencia_media_kw = total / duracion_horas if duracion_horas else None
            estadisticas["potencia_media_kw"] = potencia_media_kw
        # Para otras magnitudes, mantener promedio si existía
        elif valores:
            estadisticas["promedio"] = sum(valores)/len(valores)
        
        # Obtener la fecha real del último dato
        ultima_fecha = None
        if datos:
            # El último dato tiene la fecha más reciente
            ultimo_dato = datos[-1]
            # Convertir la hora local a fecha completa
            from datetime import datetime, timedelta
            now = datetime.now()
            hora_parts = ultimo_dato["tiempo"].split(":")
            ultima_fecha = now.replace(
                hour=int(hora_parts[0]), 
                minute=int(hora_parts[1]), 
                second=int(hora_parts[2]), 
                microsecond=0
            )
            
            # Verificar que la fecha no sea futura (problema de zona horaria)
            if ultima_fecha > now:
                # Si es futura, restar un día
                ultima_fecha = ultima_fecha - timedelta(days=1)
            
            ultima_fecha = ultima_fecha.isoformat()
        
        logger.info(f"📊 Datos procesados: {len(datos)} puntos, estadísticas: {estadisticas}")
        logger.info(f"📅 Última fecha: {ultima_fecha}")
        
        # Log adicional para diagnóstico de estado
        if datos:
            logger.info(f"🔍 Diagnóstico estado: {len(datos)} datos en rango, último dato: {datos[-1]}")
            logger.info(f"🔍 Hora actual: {datetime.now()}, Última fecha calculada: {ultima_fecha}")
        
        response_data = {
            "datos": datos,
            "estadisticas": estadisticas,
            "ultima_fecha": ultima_fecha
        }
        
        logger.info(f"✅ Respuesta API: {len(datos)} puntos de datos, actual={estadisticas['actual']}")
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"❌ Error Influx: {e}")
        return jsonify({"error": "Error consultando InfluxDB"}), 500


@app.route("/api/consumo/global")
@require_login
def api_consumo_global():
    """API para obtener datos de consumo global de todos los nodos del usuario."""
    from datetime import datetime, timedelta
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    # Obtener todos los nodos del usuario
    cur.execute(
        "SELECT N.esp_id FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s",
        (uid,),
    )
    nodos = cur.fetchall()
    if not nodos:
        return jsonify({"error": "No tienes nodos registrados"}), 404
    if not influx_client or not influx_bucket:
        return jsonify({"error": "InfluxDB no disponible"}), 503
    esp_ids = [n["esp_id"] for n in nodos]
    periodo = request.args.get('periodo', 'hora')
    now = datetime.now()
    # Definir filtro OR para múltiples esp_ids (debe estar antes de cualquier if)
    esp_filter = " or ".join([f'r["esp_id"] == "{esp_id}"' for esp_id in esp_ids])
    # Definir rango y ventana de agrupación según periodo
    if periodo == 'hora':
        # Leer parámetro de fecha (YYYY-MM-DD)
        fecha_str = request.args.get('fecha')
        if fecha_str:
            try:
                fecha = datetime.strptime(fecha_str, '%Y-%m-%d')
            except Exception:
                fecha = now
        else:
            fecha = now
        inicio_dia = fecha.replace(hour=0, minute=0, second=0, microsecond=0)
        fin_dia = fecha.replace(hour=23, minute=59, second=59, microsecond=999999)
        # Usar rango absoluto en UTC para InfluxDB
        rango_start = inicio_dia.isoformat() + 'Z'
        rango_stop = fin_dia.isoformat() + 'Z'
        group = '1h'
        labels_fmt = '%H:00'
        num_periodos = 24
        periodos_labels = [f"{i:02d}:00" for i in range(24)]
        fecha_dia = fecha.strftime('%d/%m/%Y')
    elif periodo == 'dia':
        rango = '-7d'
        group = '1d'
        labels_fmt = '%a'
        num_periodos = 7
        periodos = [(now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0) for i in reversed(range(7))]
        periodos_labels = [dt.strftime(labels_fmt) for dt in periodos]
    elif periodo == 'mes':
        rango = '-365d'
        group = '1mo'
        labels_fmt = '%b'
        num_periodos = 12
        # Generar los últimos 12 meses
        periodos = []
        for i in reversed(range(12)):
            month = (now.month - i - 1) % 12 + 1
            year = now.year - ((now.month - i - 1) // 12)
            periodos.append(datetime(year, month, 1))
        periodos_labels = [dt.strftime(labels_fmt) for dt in periodos]
    elif periodo == 'año':
        rango = '-5y'
        group = '1y'
        labels_fmt = '%Y'
        num_periodos = 5
        periodos = [datetime(now.year - i, 1, 1) for i in reversed(range(5))]
        periodos_labels = [dt.strftime(labels_fmt) for dt in periodos]
    else:
        rango = '-24h'
        group = '1h'
        labels_fmt = '%H:00'
        num_periodos = 24
        periodos = [(now - timedelta(hours=i)).replace(minute=0, second=0, microsecond=0) for i in reversed(range(24))]
        periodos_labels = [dt.strftime(labels_fmt) for dt in periodos]
    
    try:
        # Crear filtro OR para múltiples esp_ids
        # esp_filter = " or ".join([f'r["esp_id"] == "{esp_id}"' for esp_id in esp_ids]) # Moved outside
        
        # Consulta: obtener datos raw y procesarlos en Python (igual que api_nodo_magnitud)
        if periodo == 'hora':
            flux = f'''
                from(bucket: "{influx_bucket}")
                  |> range(start: {rango_start}, stop: {rango_stop})
                  |> filter(fn: (r) => r["_measurement"] == "consumo")
                  |> filter(fn: (r) => r["_field"] == "valor")
                  |> filter(fn: (r) => {esp_filter})
                  |> group()
                  |> sort(columns: ["_time"])
            '''
        else:
            flux = f'''
                from(bucket: "{influx_bucket}")
                  |> range(start: {rango})
                  |> filter(fn: (r) => r["_measurement"] == "consumo")
                  |> filter(fn: (r) => r["_field"] == "valor")
                  |> filter(fn: (r) => {esp_filter})
                  |> group()
                  |> sort(columns: ["_time"])
            '''
        
        tables = list(influx_client.query_api().query(flux))
        consumo_por_periodo = {label: 0.0 for label in periodos_labels}
        
        logger.info(f"🔍 DIAGNÓSTICO: Procesando {len(tables)} tablas de datos")
        total_registros = 0
        
        # === DIAGNÓSTICO DETALLADO ===
        logger.info("🔍 === INICIO DIAGNÓSTICO CONSUMO GLOBAL ===")
        logger.info(f"📊 Nodos consultados: {esp_ids}")
        if periodo == 'hora':
            logger.info(f"📊 Rango: {rango_start} - {rango_stop}, Agrupación: {group}")
        else:
            logger.info(f"📊 Rango: {rango}, Agrupación: {group}")
        logger.info(f"📊 Periodos labels: {periodos_labels}")
        logger.info(f"📊 Query Flux: {flux}")
        
        # Diccionario para almacenar datos por nodo y hora
        datos_por_nodo_hora = {}
        
        for table in tables:
            logger.info(f"📊 Tabla con {len(table.records)} registros")
            for record in table.records:
                total_registros += 1
                t = record.get_time()
                # Ajustar a hora local (UTC-3 para Argentina)
                try:
                    from datetime import timedelta as td
                    t = t - td(hours=3)
                except Exception:
                    pass
                
                # Obtener esp_id del registro para diagnóstico
                esp_id_record = record.values.get('esp_id', 'unknown')
                valor_record = record.get_value() or 0.0
                
                # Mapear el timestamp a la etiqueta correspondiente
                if periodo == 'hora':
                    label = t.strftime('%H:00')
                elif periodo == 'dia':
                    label = t.strftime('%a')
                elif periodo == 'mes':
                    label = t.strftime('%b')
                elif periodo == 'año':
                    label = t.strftime('%Y')
                else:
                    label = t.strftime('%H:00')
                
                # Agrupar datos por nodo y hora
                if esp_id_record not in datos_por_nodo_hora:
                    datos_por_nodo_hora[esp_id_record] = {}
                if label not in datos_por_nodo_hora[esp_id_record]:
                    datos_por_nodo_hora[esp_id_record][label] = []
                
                datos_por_nodo_hora[esp_id_record][label].append({
                    "tiempo": t,
                    "valor": valor_record
                })
                
                logger.info(f"📝 Registro {total_registros}: esp_id={esp_id_record}, timestamp={t}, valor={valor_record} - ✅ PROCESADO")
        
        # Calcular consumo por hora (suma de todos los valores de cada nodo en esa hora)
        logger.info("🔍 === CÁLCULO CONSUMO POR HORA ===")
        for esp_id in datos_por_nodo_hora:
            logger.info(f"📊 Procesando nodo: {esp_id}")
            total_nodo = 0.0
            for label in datos_por_nodo_hora[esp_id]:
                datos_hora = datos_por_nodo_hora[esp_id][label]
                if len(datos_hora) >= 1:
                    # Mostrar todos los datos de esta hora para este nodo
                    logger.info(f"  📅 Hora {label}: {len(datos_hora)} registros")
                    for i, dato in enumerate(datos_hora):
                        logger.info(f"    📝 Registro {i+1}: {dato['tiempo']} = {dato['valor']:.2f}")
                    
                    # Sumar todos los valores de consumo en esa hora para este nodo
                    consumo_hora_nodo = sum(d["valor"] for d in datos_hora)
                    total_nodo += consumo_hora_nodo
                    
                    if label in consumo_por_periodo:
                        consumo_por_periodo[label] += consumo_hora_nodo
                        logger.info(f"    ➕ Consumo hora {label}: {consumo_hora_nodo:.2f}")
                        logger.info(f"    📊 Total acumulado para {label}: {consumo_por_periodo[label]:.2f}")
                else:
                    logger.info(f"  ⚠️ Hora {label}: sin datos")
            
            logger.info(f"📊 Total nodo {esp_id}: {total_nodo:.2f}")
        
        logger.info("🔍 === RESUMEN CONSUMO POR HORA ===")
        for label in periodos_labels:
            if label in consumo_por_periodo:
                logger.info(f"📊 Hora {label}: {consumo_por_periodo[label]:.2f}")
        
        total_calculado = sum(consumo_por_periodo.values())
        logger.info(f"📊 TOTAL CALCULADO: {total_calculado:.2f}")
        
        logger.info(f"📊 TOTAL REGISTROS PROCESADOS: {total_registros}")
        
        # === COMPARACIÓN CON DATOS INDIVIDUALES ===
        logger.info("🔍 === COMPARACIÓN CON DATOS INDIVIDUALES ===")
        total_individuales = 0.0
        
        for esp_id in esp_ids:
            if periodo == 'hora':
                flux_individual = f'''
                    from(bucket: "{influx_bucket}")
                      |> range(start: {rango_start}, stop: {rango_stop})
                      |> filter(fn: (r) => r._measurement == "consumo" and r.esp_id == "{esp_id}")
                      |> filter(fn: (r) => r._field == "valor")
                      |> group()
                      |> sort(columns: ["_time"])
                '''
            else:
                flux_individual = f'''
                    from(bucket: "{influx_bucket}")
                      |> range(start: {rango})
                      |> filter(fn: (r) => r._measurement == "consumo" and r.esp_id == "{esp_id}")
                      |> filter(fn: (r) => r._field == "valor")
                      |> group()
                      |> sort(columns: ["_time"])
                '''
            
            logger.info(f"🔍 === CONSULTA INDIVIDUAL NODO {esp_id} ===")
            logger.info(f"📊 Query: {flux_individual}")
            
            result_individual = influx_client.query_api().query(flux_individual)
            datos_nodo = []
            for table in result_individual:
                logger.info(f"📊 Tabla individual {esp_id}: {len(table.records)} registros")
                for record in table.records:
                    utc_time = record.get_time()
                    from datetime import timedelta
                    local_time = utc_time - timedelta(hours=3)
                    valor = record.get_value()
                    datos_nodo.append({
                        "tiempo": local_time,
                        "valor": valor
                    })
                    logger.info(f"  📝 {esp_id}: {local_time} = {valor}")
            
            # Calcular consumo total del nodo (suma de todos los valores)
            if len(datos_nodo) >= 1:
                # Mostrar todos los valores para verificar
                logger.info(f"📊 Nodo {esp_id}: todos los valores:")
                for i, dato in enumerate(datos_nodo):
                    logger.info(f"  📝 Valor {i+1}: {dato['tiempo']} = {dato['valor']:.2f}")
                
                consumo_nodo = sum(d["valor"] for d in datos_nodo)
                total_individuales += consumo_nodo
                logger.info(f"📊 Nodo {esp_id}: consumo total={consumo_nodo:.2f} (suma de {len(datos_nodo)} registros)")
            else:
                consumo_nodo = 0.0
                logger.info(f"📊 Nodo {esp_id}: sin datos para calcular consumo")
            
            logger.info(f"📊 Nodo {esp_id}: {len(datos_nodo)} registros, consumo calculado: {consumo_nodo:.2f}")
            logger.info(f"🔍 === FIN CONSULTA INDIVIDUAL NODO {esp_id} ===")
        
        logger.info(f"📊 TOTAL INDIVIDUALES CALCULADO: {total_individuales:.2f}")
        logger.info(f"📊 TOTAL GLOBAL SUMADO: {sum(valores_validos) if 'valores_validos' in locals() else 'N/A'}")
        logger.info(f"📊 DIFERENCIA: {(sum(valores_validos) - total_individuales) if 'valores_validos' in locals() else 'N/A'}")
        logger.info("🔍 === FIN DIAGNÓSTICO ===")
        labels = periodos_labels
        valores = [consumo_por_periodo[l] for l in labels]
        unidad = "kWh"
        valores_validos = [v for v in valores if v is not None and v > 0]
        total = sum(valores_validos)
        maximo = max(valores_validos) if valores_validos else 0
        minimo = min(valores_validos) if valores_validos else 0
        estadisticas = {
            "total": f"{total:.2f}" if valores else "-",
            "maximo": f"{maximo:.2f}" if valores else "-",
            "minimo": f"{minimo:.2f}" if valores else "-"
        }
        # Solo para consumo: potencia_media_kw
        if valores_validos:
            num_periodos = len(periodos_labels)  # horas, días, meses o años generados arriba
            if periodo == 'hora':               # cada etiqueta representa 1 hora
                duracion_horas = num_periodos
            elif periodo == 'dia':              # cada etiqueta representa 1 día
                duracion_horas = num_periodos * 24
            elif periodo == 'mes':              # cada etiqueta representa 1 mes de 30 días aprox.
                duracion_horas = num_periodos * 30 * 24
            elif periodo == 'año':              # cada etiqueta representa 1 año de 365 días
                duracion_horas = num_periodos * 365 * 24
            else:
                duracion_horas = num_periodos  # caso por defecto
            potencia_media_kw = total / duracion_horas if duracion_horas else None
            estadisticas["potencia_media_kw"] = f"{potencia_media_kw:.2f}" if potencia_media_kw is not None else "-"
        
        logger.info(f"📊 Consumo por periodos: {consumo_por_periodo}")
        logger.info(f"📊 Labels: {labels}")
        logger.info(f"📊 Valores: {valores}")
        logger.info(f"📊 Total global: {total:.2f} kWh")
        logger.info(f"📊 Estadísticas: {estadisticas}")
        
        response_data = {
            "labels": labels,
            "valores": valores,
            "unidad": unidad,
            "estadisticas": estadisticas,
            "periodo": periodo,
            "num_nodos": len(nodos)
        }
        
        # Agregar fecha si es consulta por hora
        if periodo == 'hora':
            response_data["fecha"] = fecha_dia
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error consultando InfluxDB: {e}")
        return jsonify({"error": "Error consultando InfluxDB"}), 500

@app.route("/api/kpi-global")
@require_login
def api_kpi_global():
    uid = session["user_id"]
    cur = mysql.connection.cursor()
    cur.execute("SELECT N.esp_id FROM Nodos N JOIN UsuariosNodos UN USING(nodo_id) WHERE UN.usuario_id=%s", (uid,))
    esp_ids = [r["esp_id"] for r in cur.fetchall()]
    
    logger.info(f"🔍 Consultando KPIs globales: esp_ids={esp_ids}")
    
    if not influx_client or not influx_bucket:
        return jsonify({"error": "InfluxDB no disponible"}), 503
    # Consulta real (últimos 30 min) - usando filtro OR para múltiples esp_ids
    if not esp_ids:
        return jsonify({"error": "No hay nodos registrados"}), 404
    
    # Crear filtro OR para múltiples esp_ids
    esp_filter = " or ".join([f'r["esp_id"] == "{esp_id}"' for esp_id in esp_ids])
    
    flux = f'''
        from(bucket: "{influx_bucket}")
          |> range(start: -30m)
          |> filter(fn: (r) => {esp_filter})
          |> filter(fn: (r) => r["_measurement"] == "tension" or r["_measurement"] == "corriente" or r["_measurement"] == "consumo" or r["_measurement"] == "fp" or r["_measurement"] == "frecuencia")
          |> filter(fn: (r) => r["_field"] == "valor")
          |> group(columns: ["esp_id", "_measurement"])
          |> mean()
          |> group(columns: ["_measurement"])
          |> mean()
          |> yield(name: "mean")
    '''
    
    logger.info(f"📊 Query Flux KPI: {flux}")
    
    try:
        tables = list(influx_client.query_api().query(flux))
        logger.info(f"📋 Número de tablas KPI: {len(tables)}")
        
        result = {"tension": None, "corriente": None, "consumo": None, "fp": None, "frecuencia": None}
        for i, table in enumerate(tables):
            logger.info(f"📊 Tabla KPI {i}: {len(table.records)} registros")
            for record in table.records:
                field = record.get_measurement()  # Cambiado de get_field() a get_measurement()
                value = record.get_value()
                logger.info(f"  📝 KPI: measurement={field}, valor={value}")
                if field in result:
                    result[field] = value
        
        logger.info(f"📈 Resultado KPI: {result}")
        
        # Si todos los valores son None, no hay datos recientes
        if all(v is None for v in result.values()):
            logger.warning("⚠️ No hay datos recientes para KPIs")
            return jsonify({"error": "Sin datos recientes"}), 404
        # Si algún valor es None, poner '-'
        for k in result:
            if result[k] is None:
                result[k] = "-"
        result["sin_datos"] = False
        logger.info(f"✅ KPIs finales: {result}")
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error consultando InfluxDB KPI: {e}")
        return jsonify({"error": "Error consultando InfluxDB"}), 500

# =====================
# WORKER MQTT GLOBAL → INFLUXDB
# =====================

def start_global_mqtt_worker():
    """
    Inicia un único worker global que se suscribe a enertrack/# y almacena todos los datos recibidos en InfluxDB.
    """
    import influxdb_client
    from influxdb_client.client.write_api import SYNCHRONOUS
    from aiomqtt import Client
    import asyncio

    INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
    INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "token")
    INFLUX_ORG = os.getenv("INFLUX_ORG", "org")
    INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "medidoresEnergia")

    write_api = None
    client_influx = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client_influx = influxdb_client.InfluxDBClient(
            url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG
        )
        write_api = client_influx.write_api(write_options=SYNCHRONOUS)
        logger.info(f"✅ Conectado a InfluxDB: {INFLUX_URL}")
    except Exception as e:
        logger.error(f"❌ Error conectando a InfluxDB: {e}")
        return

    async def mqtt_worker():
        try:
            async with Client(
                MQTT_DOMINIO,
                port=MQTT_PORT,
                username=MQTT_USER,
                password=MQTT_PASS,
                tls_context=ssl_context,
            ) as mqtt:
                await mqtt.subscribe("enertrack/#")
                logger.info(f"Suscrito a tópico MQTT global: enertrack/#")
                async for message in mqtt.messages:
                    topic = str(message.topic)
                    payload = message.payload.decode()
                    # Extraer esp_id y magnitud del topic: enertrack/<esp_id>/<magnitud>
                    m = re.match(r"enertrack/([^/]+)/([^/]+)", topic)
                    if not m:
                        logger.warning(f"Tópico no reconocido: {topic}")
                        continue
                    esp_id, mag = m.group(1), m.group(2)
                    try:
                        valor = float(payload)
                    except ValueError:
                        logger.warning(f"Payload inválido para {mag}: '{payload}' en {topic}")
                        continue
                    punto = influxdb_client.Point(mag).tag("esp_id", esp_id).field("valor", valor)
                    try:
                        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=punto)
                        logger.info(f"✅ Dato guardado en InfluxDB: {topic}={payload}")
                    except Exception as e:
                        logger.error(f"❌ Error escribiendo en InfluxDB: {e}")
                        logger.error(f"  Bucket: {INFLUX_BUCKET}, Org: {INFLUX_ORG}")
        except Exception as e:
            logger.error(f"Error en worker MQTT global: {e}")
        finally:
            if client_influx:
                client_influx.close()

    def run():
        loop.run_until_complete(mqtt_worker())

    hilo = threading.Thread(target=run, daemon=True)
    hilo.start()
    logger.info(f"Worker MQTT global iniciado")

# Iniciar el worker global al arrancar la aplicación
start_global_mqtt_worker()

# Eliminar funciones y lógica de workers por nodo y active_workers

# Iniciar workers para todos los nodos al arrancar la aplicación
# Esto se ejecuta independientemente de cómo se inicie la app (directo o con Gunicorn)
def initialize_workers():
    """Inicializa los workers al arrancar la aplicación"""
    logger.info("🚀 Inicializando workers MQTT al arrancar la aplicación...")
    def run_with_context():
        with app.app_context():
            # start_all_workers() # Eliminado
            pass # No hay workers por nodo para iniciar
    init_thread = threading.Thread(target=run_with_context, daemon=True)
    init_thread.start()
    logger.info("✅ Hilo de inicialización de workers iniciado")

# Inicializar workers cuando se importa el módulo
initialize_workers()

# Función para verificación periódica de workers
def start_worker_monitor():
    """Inicia un monitor que verifica periódicamente el estado de los workers"""
    def monitor():
        while True:
            try:
                time.sleep(60)  # Verificar cada minuto
                # check_and_restart_workers() # Eliminado
            except Exception as e:
                logger.error(f"❌ Error en monitor de workers: {e}")
    
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    logger.info("🔍 Monitor de workers iniciado")

# Iniciar monitor de workers
start_worker_monitor()

# --- INICIO INTEGRACIÓN TELEGRAM ---
import secrets
from flask import jsonify, request
from telegram_bot import start_bot_in_thread, pending_links

# Arrancar el bot de Telegram en un hilo al iniciar la app
start_bot_in_thread()

@app.route('/api/telegram/generate_link_code', methods=['POST'])
@require_login
def generate_telegram_link_code():
    try:
        usuario_id = session['user_id']
        cur = mysql.connection.cursor()
        cur.execute("SELECT telegram_chat_id FROM Usuarios WHERE usuario_id = %s", (usuario_id,))
        row = cur.fetchone()
        if row and row['telegram_chat_id']:
            return jsonify({'error': 'Tu cuenta ya está vinculada con Telegram.'}), 400
        logger.info(f"[TELEGRAM] Generando código de vinculación para usuario_id={usuario_id}")
        # Generar código único de 8 caracteres
        code = secrets.token_urlsafe(6)
        logger.info(f"[TELEGRAM] Código generado: {code}")
        # Guardar en el diccionario temporal (en producción, usar DB o caché persistente)
        pending_links[code] = usuario_id
        logger.info(f"[TELEGRAM] pending_links actualizado: {pending_links}")
        # Username real del bot
        bot_username = 'enerTrackBot'
        telegram_link = f'https://t.me/{bot_username}?start={code}'
        logger.info(f"[TELEGRAM] Enlace generado: {telegram_link}")
        return jsonify({'code': code, 'telegram_link': telegram_link})
    except Exception as e:
        logger.error(f"[TELEGRAM] Error generando código de vinculación: {e}")
        return jsonify({'error': 'Error generando código de vinculación'}), 500

@app.route('/api/telegram/vincular', methods=['POST'])
def telegram_vincular():
    try:
        data = request.get_json()
        code = data.get('code')
        chat_id = data.get('chat_id')
        if not code or not chat_id:
            return jsonify({'error': 'Faltan parámetros'}), 400
        from telegram_bot import pending_links
        usuario_id = pending_links.get(code)
        if not usuario_id:
            return jsonify({'error': 'El código de vinculación es inválido o ha expirado.'}), 400
        cur = mysql.connection.cursor()
        cur.execute("SELECT telegram_chat_id FROM Usuarios WHERE usuario_id = %s", (usuario_id,))
        row = cur.fetchone()
        if row and row['telegram_chat_id']:
            return jsonify({'error': 'Tu cuenta ya está vinculada con Telegram.'}), 400
        cur.execute("UPDATE Usuarios SET telegram_chat_id = %s WHERE usuario_id = %s", (chat_id, usuario_id))
        mysql.connection.commit()
        del pending_links[code]
        return jsonify({'success': True}), 200
    except Exception as e:
        mysql.connection.rollback()
        logger.error(f"Error al guardar chat_id: {e}")
        return jsonify({'error': 'Ocurrió un error al vincular tu cuenta.'}), 500

def send_telegram_alert(chat_id, message):
    try:
        token = os.environ.get('enertrackBotToken')
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = {'chat_id': chat_id, 'text': message}
        resp = pyrequests.post(url, data=data, timeout=10)
        if resp.status_code != 200:
            logger.error(f"[TELEGRAM] Error enviando alerta: {resp.text}")
    except Exception as e:
        logger.error(f"[TELEGRAM] Excepción enviando alerta: {e}")


def job_alertas_consumo():
    logger.info('[ALERTAS] Ejecutando job de alertas de consumo...')
    with app.app_context():
        try:
            cur = mysql.connection.cursor()
            cur.execute("""
                SELECT U.nodo_id, U.umbral_kw, U.estado_alerta, U.ultima_alerta, N.esp_id, N.descripcion, UN.usuario_id, USU.telegram_chat_id
                FROM UmbralesNodo U
                JOIN Nodos N ON U.nodo_id = N.nodo_id
                JOIN UsuariosNodos UN ON UN.nodo_id = N.nodo_id
                JOIN Usuarios USU ON USU.usuario_id = UN.usuario_id
                WHERE U.umbral_kw IS NOT NULL AND USU.telegram_chat_id IS NOT NULL
            """)
            umbrales = cur.fetchall()
            for u in umbrales:
                nodo_id = u['nodo_id']
                umbral_kw = float(u['umbral_kw'])
                estado_alerta = u['estado_alerta']
                ultima_alerta = u['ultima_alerta']
                esp_id = u['esp_id']
                descripcion = u['descripcion']
                usuario_id = u['usuario_id']
                chat_id = u['telegram_chat_id']
                now = datetime.now(pytz.UTC)
                try:
                    if not influx_client or not influx_bucket:
                        logger.warning('[ALERTAS] InfluxDB no disponible, omitiendo nodo %s', esp_id)
                        continue
                    # Ventana fija de 15 minutos, sumando incrementos cada 2 minutos
                    flux = f'''
                        from(bucket: "{influx_bucket}")
                          |> range(start: -15m)
                          |> filter(fn: (r) => r._measurement == "consumo" and r.esp_id == "{esp_id}")
                          |> filter(fn: (r) => r._field == "valor")
                          |> aggregateWindow(every: 2m, fn: sum)
                          |> yield(name: "sum")
                    '''
                    result = influx_client.query_api().query(flux)
                    datos = []
                    total_registros = sum(1 for table in result for _ in table.records)
                    # Reiniciar el iterador porque ya lo recorrimos
                    result = influx_client.query_api().query(flux)
                    for table in result:
                        for record in table.records:
                            v = record.get_value()
                            if v is not None:
                                datos.append(float(v))
                    logger.info(f"[ALERTAS] Nodo {esp_id}: {len(datos)}/{total_registros} registros con valor numérico")
                    if not datos:
                        logger.warning(f"[ALERTAS] Sin datos válidos para nodo {esp_id} en la ventana. Se omite.")
                        continue
                    consumo_ventana_kwh = sum(datos)  # solo números; no lanzará TypeError
                    potencia_media_kw = consumo_ventana_kwh / 0.25  # 15 min = 0,25 h
                    logger.info(f"[ALERTAS] Nodo {esp_id}: potencia_media={potencia_media_kw:.3f} kW (umbral={umbral_kw} kW)")
                    # Histeresis
                    if potencia_media_kw >= umbral_kw and estado_alerta == 0:
                        hora = now.astimezone(pytz.timezone('America/Argentina/Buenos_Aires')).strftime('%H:%M')
                        mensaje_alerta = (
                            f"⚠️ Potencia media de los últimos 15\u202Fminutos: "
                            f"{potencia_media_kw:.2f}\u202FkW (límite {umbral_kw:.2f}\u202FkW)\n"
                            f"• Nodo: {descripcion or 'Sin descripción'} (ESP {esp_id})"
                        )
                        send_telegram_alert(chat_id, mensaje_alerta)
                        cur2 = mysql.connection.cursor()
                        cur2.execute("UPDATE UmbralesNodo SET estado_alerta=1, ultima_alerta=NOW() WHERE nodo_id=%s AND usuario_id=%s", (nodo_id, usuario_id))
                        mysql.connection.commit()
                        logger.info(f"[ALERTAS] Alerta enviada a usuario {usuario_id} para nodo {esp_id}")
                    elif potencia_media_kw < 0.8 * umbral_kw and estado_alerta == 1:
                        cur2 = mysql.connection.cursor()
                        cur2.execute("UPDATE UmbralesNodo SET estado_alerta=0 WHERE nodo_id=%s AND usuario_id=%s", (nodo_id, usuario_id))
                        mysql.connection.commit()
                        logger.info(f"[ALERTAS] Rearme de alerta para nodo {esp_id}")
                except Exception as e:
                    logger.error(f"[ALERTAS] Excepción procesando nodo {esp_id}: {e}")
        except Exception as e:
            logger.error(f"[ALERTAS] Excepción general en job de alertas: {e}")

# Iniciar el scheduler al arrancar la app
scheduler = BackgroundScheduler()
scheduler.add_job(job_alertas_consumo, 'interval', minutes=5, next_run_time=datetime.now()+timedelta(seconds=10))
scheduler.start()
logger.info('[ALERTAS] Scheduler de alertas iniciado')
# --- FIN INTEGRACIÓN TELEGRAM ---

@app.route('/enertrack/api/umbral/<int:nodo_id>', methods=['GET'])
@require_login
def get_umbral_nodo(nodo_id):
    uid = session['user_id']
    cur = mysql.connection.cursor()
    try:
        logger.info(f"[UMBRAL][GET] Usuario {uid} consulta umbral de nodo {nodo_id}")
        cur.execute("SELECT umbral_kw FROM UmbralesNodo WHERE nodo_id=%s AND usuario_id=%s", (nodo_id, uid))
        row = cur.fetchone()
        if row:
            logger.info(f"[UMBRAL][GET] Respuesta: umbral_kw={row['umbral_kw']}")
            return jsonify({'umbral_kw': float(row['umbral_kw'])})
        else:
            logger.info(f"[UMBRAL][GET] Sin umbral definido para nodo {nodo_id} y usuario {uid}")
            return jsonify({'umbral_kw': None})
    except Exception as e:
        logger.error(f"[UMBRAL][GET] Error: {e}")
        return jsonify({'umbral_kw': None, 'error': str(e)}), 500

@app.route('/enertrack/api/umbral/<int:nodo_id>', methods=['POST'], endpoint='set_umbral_nodo')
@require_login
def set_umbral_nodo(nodo_id):
    uid = session['user_id']
    try:
        data = request.get_json()
        logger.info(f"[UMBRAL][POST] Usuario {uid} setea umbral nodo {nodo_id}: {data}")
        umbral_kw = float(data.get('umbral_kw'))
        cur = mysql.connection.cursor()
        cur.execute("SELECT 1 FROM UmbralesNodo WHERE nodo_id=%s AND usuario_id=%s", (nodo_id, uid))
        if cur.fetchone():
            cur.execute("UPDATE UmbralesNodo SET umbral_kw=%s WHERE nodo_id=%s AND usuario_id=%s", (umbral_kw, nodo_id, uid))
        else:
            cur.execute("INSERT INTO UmbralesNodo (nodo_id, usuario_id, umbral_kw) VALUES (%s, %s, %s)", (nodo_id, uid, umbral_kw))
        mysql.connection.commit()
        logger.info(f"[UMBRAL][POST] Umbral guardado para nodo {nodo_id} y usuario {uid}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"[UMBRAL][POST] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/enertrack/api/umbral/<int:nodo_id>', methods=['DELETE'])
@require_login
def delete_umbral_nodo(nodo_id):
    uid = session['user_id']
    try:
        logger.info(f"[UMBRAL][DELETE] Usuario {uid} elimina umbral nodo {nodo_id}")
        cur = mysql.connection.cursor()
        cur.execute("DELETE FROM UmbralesNodo WHERE nodo_id=%s AND usuario_id=%s", (nodo_id, uid))
        mysql.connection.commit()
        logger.info(f"[UMBRAL][DELETE] Umbral eliminado para nodo {nodo_id} y usuario {uid}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"[UMBRAL][DELETE] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/perfil')
@require_login
def perfil():
    uid = session['user_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT nombreUsuario, telegram_chat_id FROM Usuarios WHERE usuario_id=%s", (uid,))
    row = cur.fetchone()
    username = row['nombreUsuario'] if row else ''
    telegram_vinculado = bool(row and row['telegram_chat_id'])
    return render_template('perfil.html', username=username, telegram_vinculado=telegram_vinculado)

@app.route('/api/telegram/unlink', methods=['POST'])
@require_login
def telegram_unlink():
    try:
        usuario_id = session['user_id']
        cur = mysql.connection.cursor()
        cur.execute("SELECT telegram_chat_id FROM Usuarios WHERE usuario_id = %s", (usuario_id,))
        row = cur.fetchone()
        if not row or not row['telegram_chat_id']:
            return jsonify({'error': 'No tienes una cuenta de Telegram vinculada.'}), 400
        cur.execute("UPDATE Usuarios SET telegram_chat_id = NULL WHERE usuario_id = %s", (usuario_id,))
        mysql.connection.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        mysql.connection.rollback()
        logger.error(f"[TELEGRAM] Error desvinculando Telegram: {e}")
        return jsonify({'error': 'Error al desvincular la cuenta de Telegram.'}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8006)), debug=True)
