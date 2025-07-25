# ENERTRACK – Plataforma de Monitoreo y Gestión de Consumo Energético

## Descripción general

EnerTrack es una plataforma web escrita en Python (Flask) que permite monitorear, analizar y optimizar el consumo eléctrico en tiempo real mediante medidores inteligentes basados en ESP32. Su enfoque es brindar a hogares, empresas e instituciones una visión clara y útil de su uso energético para reducir costos y mejorar la eficiencia.

## Características principales

* Gestión de usuarios (registro, inicio de sesión y autenticación segura con scrypt).
* Gestión de nodos ESP32 (alta, edición y baja lógica).
* Asignación muchos‑a‑muchos de nodos a usuarios mediante la tabla **UsuariosNodos** con campos `activo`, `ubicacion`, `fecha_asignacion` y `ultimo_acceso`.
* KPIs globales (tensión, corriente, energía, factor de potencia, frecuencia).
* Dashboards individuales con históricos interactivos.
* Análisis de consumo agregado por hora, día, mes y año.
* Detección automática de nodos activos, inactivos o sin datos recientes.
* Tema claro/oscuro y notificaciones amigables.
* **Alertas automáticas por Telegram**: recibe notificaciones en tiempo real cuando la potencia media de tus nodos supera el umbral configurado.

## Integración con Telegram

EnerTrack incluye un bot de Telegram que permite a los usuarios recibir alertas automáticas sobre el consumo energético de sus nodos. Las principales funcionalidades son:

- **Vinculación segura**: cada usuario puede vincular su cuenta de EnerTrack con su cuenta de Telegram mediante un código único generado desde la web.
- **Alertas automáticas**: si la potencia media de un nodo en los últimos 15 minutos supera el umbral de kW configurado por el usuario, el sistema envía una alerta inmediata por Telegram.
- **Gestión de umbrales**: cada usuario puede definir y modificar el umbral de potencia para cada uno de sus nodos desde la plataforma web.
- **Privacidad y seguridad**: solo los usuarios que hayan vinculado correctamente su cuenta pueden recibir alertas y comunicarse con el bot.

### ¿Cómo funciona la vinculación?

1. El usuario accede a la sección de perfil o gestión de nodos y solicita vincular su cuenta con Telegram.
2. El sistema genera un enlace y un código único para el usuario.
3. El usuario abre el enlace y se envía el código automáticamente en la url para completar la vinculación.
4. Una vez vinculado, el usuario recibirá alertas automáticas cuando alguno de sus nodos supere el umbral de potencia configurado.

## Arquitectura y tecnologías

* **Backend:** Python 3.11, Flask, Flask‑MySQLdb
* **Base relacional:** MariaDB/MySQL (usuarios, nodos, UsuariosNodos)
* **Serie temporal:** InfluxDB v2 (mediciones)
* **Mensajería IoT:** MQTT sobre TLS
* **Frontend:** HTML5, Bootstrap 5, Chart.js
* **Contenedores:** Docker (opcional)
* **Notificaciones:** Bot de Telegram integrado

## Estructura del proyecto

```	enertrack/
├── app.py                 # Lógica principal y worker MQTT
├── telegram_bot.py        # Lógica del bot de Telegram y vinculación
├── templates/             # Vistas HTML (Jinja2)
├── static/                # JS, CSS, imágenes
├── requirements.txt
└── Dockerfile
```

## Script SQL clave (`UsuariosNodos`)

```sql
CREATE TABLE `UsuariosNodos` (
  `usuario_id`      INT(10) UNSIGNED NOT NULL,
  `nodo_id`         INT(10) UNSIGNED NOT NULL,
  `activo`          TINYINT(1) NOT NULL DEFAULT 1,
  `ubicacion`       VARCHAR(100) DEFAULT NULL,
  `fecha_asignacion` DATETIME DEFAULT CURRENT_TIMESTAMP(),
  `ultimo_acceso`   DATETIME DEFAULT NULL,
  PRIMARY KEY (`usuario_id`, `nodo_id`),
  FOREIGN KEY (`usuario_id`) REFERENCES Usuarios(id),
  FOREIGN KEY (`nodo_id`)   REFERENCES Nodos(id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_uca1400_ai_ci;
```

## Flujo de funcionamiento

1. Registro e inicio de sesión.
2. Asignación y gestión de nodos (tabla UsuariosNodos).
3. Recepción de datos MQTT en los tópicos `enertrack/<ESPID>/...` y almacenamiento en InfluxDB.
4. Visualización: panel global, dashboards por nodo y análisis de consumo.
5. Actualización automática de `ultimo_acceso` y detección de inactividad.
6. Interfaz adaptable con tema claro/oscuro.
7. **Recepción de alertas automáticas por Telegram cuando se superan los umbrales de potencia.**

## Dependencias y requisitos

* Python ≥ 3.11
* MariaDB/MySQL
* InfluxDB v2
* Broker MQTT con TLS
* Paquetes Python (ver `requirements.txt`): Flask, flask-mysqldb, gunicorn, paho-mqtt, aiomqtt, influxdb-client, cryptography, certifi, python-telegram-bot, mysql-connector-python.

## Despliegue

**Variables de entorno mínimas**

```
MYSQL_USER      MYSQL_PASSWORD   MYSQL_DB   MYSQL_HOST
INFLUX_URL      INFLUX_TOKEN     INFLUX_ORG INFLUX_BUCKET
DOMINIO         PUERTO_MQTTS     MQTT_USR   MQTT_PASS
FLASK_SECRET_KEY
enertrackBotToken   # Token del bot de Telegram
```

**Instalación manual (entorno de desarrollo)**

```bash
sudo apt update && sudo apt install -y python3-dev default-libmysqlclient-dev build-essential pkg-config
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py         # puerto 8006 por defecto
```

**Despliegue con Docker (producción)**

```bash
docker build -t enertrack .
docker run -d --env-file .env -p 8006:8006 --name enertrack enertrack
# Aplicación en http://localhost:8006/
```

## Experiencia de usuario

* Panel principal con KPIs y estado de nodos.
* Dashboards interactivos por magnitud y nodo.
* Estadísticas de consumo agregadas.
* Formularios intuitivos con validación.
* Alternancia entre tema claro y oscuro.
* **Recepción de alertas inmediatas por Telegram cuando se superan los umbrales de potencia configurados.**

## Notas técnicas y seguridad

* Contraseñas hasheadas con scrypt.
* Consultas SQL parametrizadas.
* Modo demo con datos ficticios si faltan InfluxDB o MQTT.
* Supervisión de workers MQTT con reintento automático.
* **El bot de Telegram solo permite el acceso a usuarios autenticados y vinculados, garantizando la privacidad de las alertas.**

## Contacto y soporte

Para dudas o mejoras, abre un *issue* en el repositorio o contacta al mantenedor.

Gracias por usar EnerTrack.
