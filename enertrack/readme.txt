ENERTRACK – Plataforma de Monitoreo y Gestión de Consumo Energético
===============================================================

Descripción General
-------------------
EnerTrack es una plataforma web desarrollada en Python (Flask) que permite a usuarios monitorear, analizar y gestionar el consumo energético de múltiples dispositivos (nodos) en tiempo real. Está orientada a instalaciones con medidores inteligentes basados en ESP32, y es ideal para hogares, empresas, instituciones educativas o cualquier entorno donde se requiera visualizar y optimizar el uso de la energía eléctrica.

Características Principales
--------------------------
- **Gestión de usuarios:** Registro, inicio de sesión y autenticación segura.
- **Gestión de nodos:** Alta, edición y baja de dispositivos de medición (nodos ESP32).
- **Visualización de KPIs:** Panel principal con indicadores clave (tensión, corriente, consumo, factor de potencia y frecuencia) promediados de todos los nodos.
- **Dashboards individuales:** Visualización detallada de cada nodo, con gráficos históricos y métricas de cada magnitud.
- **Análisis global de consumo:** Gráficos y estadísticas agregadas por hora, día, mes y año para todos los nodos del usuario.
- **Estado de dispositivos:** Detección automática de nodos activos, desconectados o sin datos recientes.
- **Tema claro/oscuro:** Interfaz moderna y adaptable a preferencias del usuario.
- **Notificaciones y validaciones amigables:** Mensajes claros para acciones exitosas o errores.

Arquitectura y Tecnologías
--------------------------
- **Backend:** Python 3.11, Flask, Flask-MySQLdb
- **Frontend:** HTML5, Bootstrap 5, Chart.js, JavaScript moderno
- **Base de datos relacional:** MariaDB/MySQL (usuarios, nodos, relaciones)
- **Base de datos de series temporales:** InfluxDB v2 (almacenamiento de mediciones)
- **Mensajería IoT:** MQTT sobre TLS (recepción de datos desde los ESP32)
- **Docker:** Contenedor para despliegue sencillo

Estructura del Proyecto
-----------------------
- `app.py`: Lógica principal del backend, rutas, API, workers MQTT, gestión de usuarios y nodos.
- `templates/`: Plantillas HTML para todas las vistas (panel, dashboards, login, registro, etc.).
- `static/`: Archivos estáticos (JS, CSS, imágenes). Incluye scripts para tema claro/oscuro y validaciones.
- `requirements.txt`: Dependencias de Python necesarias.
- `Dockerfile`: Imagen lista para producción.

Flujo de Funcionamiento
-----------------------
1. **Registro e inicio de sesión:** Los usuarios pueden crear una cuenta y acceder a su panel personal.
2. **Gestión de nodos:** Cada usuario puede agregar, editar o eliminar nodos (medidores ESP32) indicando su ID, descripción y ubicación.
3. **Recepción de datos:** Los nodos ESP32 envían datos de tensión, corriente, consumo, factor de potencia y frecuencia vía MQTT. El backend escucha estos mensajes y los almacena en InfluxDB.
4. **Visualización:**
   - El panel principal muestra KPIs globales y el estado de todos los nodos.
   - Dashboards individuales permiten analizar cada magnitud con gráficos interactivos.
   - El análisis global de consumo permite comparar el uso energético por hora, día, mes y año.
5. **Estado de nodos:** El sistema detecta automáticamente si un nodo está activo (datos recientes), desconectado o sin datos.
6. **Tema claro/oscuro:** El usuario puede alternar entre modos visuales modernos.

Dependencias y Requisitos
-------------------------
- Python >= 3.11
- MariaDB/MySQL
- InfluxDB v2
- MQTT broker (con TLS)
- Requisitos Python (ver `requirements.txt`):
  - Flask
  - flask-mysqldb
  - gunicorn
  - cryptography
  - paho-mqtt
  - influxdb-client
  - aiomqtt
  - certifi

Despliegue y Ejecución
----------------------
### 1. Variables de entorno necesarias
- `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`, `MYSQL_HOST`: Configuración de la base de datos relacional.
- `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET`: Configuración de InfluxDB.
- `DOMINIO`, `PUERTO_MQTTS`, `MQTT_USR`, `MQTT_PASS`: Configuración del broker MQTT.
- `FLASK_SECRET_KEY`: Clave secreta para sesiones Flask.

### 2. Instalación manual
```bash
# Instalar dependencias del sistema
sudo apt-get update && sudo apt-get install python3-dev default-libmysqlclient-dev build-essential pkg-config

# Instalar dependencias de Python
pip install -r requirements.txt

# Ejecutar la aplicación (modo desarrollo)
python app.py
```

### 3. Despliegue con Docker
```bash
docker build -t enertrack .
docker run -d -p 8006:8006 --env-file .env enertrack
```

La aplicación quedará disponible en http://localhost:8006/enertrack

Experiencia de Usuario
----------------------
- **Panel principal:** Resumen visual de todos los nodos, KPIs globales y acceso rápido a dashboards y gestión de nodos.
- **Dashboards:** Gráficos interactivos para cada magnitud (tensión, corriente, consumo, factor de potencia, frecuencia) con métricas de valor actual, máximo, mínimo y última actualización.
- **Análisis global:** Visualización del consumo energético total por diferentes periodos, con estadísticas agregadas.
- **Gestión intuitiva:** Formularios claros para agregar/editar nodos, validaciones en tiempo real y mensajes de ayuda.
- **Tema adaptable:** Alternancia entre modo claro y oscuro según preferencia del usuario.

Notas Técnicas y Seguridad
--------------------------
- Las contraseñas se almacenan de forma segura usando hash scrypt.
- El sistema soporta múltiples usuarios y nodos por usuario.
- Si InfluxDB o MQTT no están configurados, la app puede funcionar en modo "demo" con datos ficticios.
- El backend implementa workers en hilos para recibir datos MQTT de cada nodo y almacenarlos en InfluxDB.
- El monitoreo de workers es automático: si un hilo falla, se reinicia.

Contacto y Soporte
------------------
Para dudas, soporte o sugerencias, contactar al desarrollador principal o abrir un issue en el repositorio correspondiente.

¡Gracias por usar EnerTrack! 