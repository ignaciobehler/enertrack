# Sistema de Control de Nodos IoT con Flask y MQTT

Este proyecto es una aplicación web desarrollada en Flask que permite gestionar y controlar nodos IoT a través de brokers MQTT.

## Características Principales

### 1. Sistema de Autenticación
- Registro de usuarios con contraseñas hasheadas
- Login seguro con sesiones
- Protección de rutas mediante decorador @require_login
- Cierre de sesión automático después de 180 segundos de inactividad

### 2. Gestión de Brokers MQTT
- Registro de múltiples brokers MQTT por usuario
- Almacenamiento seguro de credenciales MQTT (contraseñas cifradas con Fernet)
- Configuración de dominio, puerto TLS, usuario y contraseña
- Edición de brokers existentes (modificación de todos los parámetros)
- Eliminación de brokers (solo si no tienen nodos asociados)

### 3. Gestión de Nodos IoT
- Registro de nodos asociados a brokers
- Identificación única por raspberry_id
- Descripción personalizada para cada nodo
- Eliminación de nodos

### 4. Control de Nodos
- Interfaz web para controlar nodos individuales
- Dos tipos de acciones:
  * Destello : Envía señal de destello al nodo
  * Setpoint: Envía un valor numérico al nodo
- Comunicación segura mediante MQTT con TLS

## Requisitos Técnicos

### Dependencias Principales
- Flask: Framework web
- Flask-MySQLdb: Conexión a base de datos MySQL
- paho-mqtt: Cliente MQTT
- cryptography: Para cifrado de contraseñas
- Werkzeug: Para manejo de sesiones y seguridad

### Variables de Entorno Requeridas
- FLASK_SECRET_KEY: Clave secreta para sesiones
- MYSQL_USER: Usuario de MySQL
- MYSQL_PASSWORD: Contraseña de MySQL
- MYSQL_DB: Nombre de la base de datos
- MYSQL_HOST: Host de MySQL
- FERNET_KEY: Clave para cifrado de contraseñas MQTT

## Estructura de la Base de Datos

La base de datos `nodosRemotos` utiliza el motor InnoDB con codificación UTF-8 y se ha creado un usuario especial con acceso exclusivo a esta base de datos para mayor seguridad.

### Tabla: Usuarios
```sql
CREATE TABLE `Usuarios` (
  `usuario_id` int(10) UNSIGNED NOT NULL AUTO_INCREMENT,
  `nombreUsuario` varchar(100) NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `tema` tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`usuario_id`),
  UNIQUE KEY `nombreUsuario` (`nombreUsuario`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

### Tabla: BrokersUsuario
```sql
CREATE TABLE `BrokersUsuario` (
  `broker_id` int(10) UNSIGNED NOT NULL AUTO_INCREMENT,
  `usuario_id` int(10) UNSIGNED NOT NULL,
  `dominio` varchar(100) NOT NULL,
  `mqtt_usr` varchar(100) NOT NULL,
  `mqtt_pass` varchar(255) NOT NULL,
  `puerto_tls` smallint(5) UNSIGNED NOT NULL,
  PRIMARY KEY (`broker_id`),
  UNIQUE KEY `usuario_id` (`usuario_id`,`dominio`),
  FOREIGN KEY (`usuario_id`) REFERENCES `Usuarios` (`usuario_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

### Tabla: Nodos
```sql
CREATE TABLE `Nodos` (
  `nodo_id` int(10) UNSIGNED NOT NULL AUTO_INCREMENT,
  `raspberry_id` varchar(20) NOT NULL,
  `descripcion` varchar(200) NOT NULL,
  `broker_id` int(10) UNSIGNED NOT NULL,
  PRIMARY KEY (`nodo_id`),
  UNIQUE KEY `uq_nodo_por_broker` (`broker_id`,`raspberry_id`),
  FOREIGN KEY (`broker_id`) REFERENCES `BrokersUsuario` (`broker_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

### Tabla: posee (Relación muchos a muchos)
```sql
CREATE TABLE `posee` (
  `usuario_id` int(10) UNSIGNED NOT NULL,
  `nodo_id` int(10) UNSIGNED NOT NULL,
  PRIMARY KEY (`usuario_id`,`nodo_id`),
  FOREIGN KEY (`usuario_id`) REFERENCES `Usuarios` (`usuario_id`) ON DELETE CASCADE,
  FOREIGN KEY (`nodo_id`) REFERENCES `Nodos` (`nodo_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
```

### Relaciones y Restricciones
- Cada usuario puede tener múltiples brokers
- Cada broker puede tener múltiples nodos
- La relación `posee` permite asignar nodos a múltiples usuarios
- Se mantiene la integridad referencial con eliminación en cascada
- Los nombres de usuario son únicos
- No se pueden tener nodos duplicados en el mismo broker
- No se pueden tener brokers duplicados para el mismo usuario

## Flujo de Uso

1. **Registro y Login**
   - El usuario se registra con nombre de usuario y contraseña
   - Inicia sesión para acceder al sistema

2. **Gestión de Brokers**
   - Agrega brokers MQTT con sus credenciales
   - Visualiza lista de brokers disponibles
   - Elimina brokers no utilizados

3. **Gestión de Nodos**
   - Agrega nodos asociados a un broker
   - Asigna identificador único y descripción
   - Visualiza lista de nodos por broker

4. **Control de Nodos**
   - Selecciona un nodo para controlar
   - Envía comandos de destello o setpoint
   - Recibe confirmación de acciones

## Seguridad

- Contraseñas de usuarios hasheadas con scrypt
- Contraseñas MQTT cifradas con Fernet
- Sesiones seguras con tiempo de expiración
- Comunicación MQTT sobre TLS
- Validación de permisos en todas las operaciones

## Logging

El sistema mantiene un registro detallado de:
- Registro de usuarios
- Inicios y cierres de sesión
- Operaciones con brokers
- Operaciones con nodos
- Errores y excepciones

## Red Docker iot-net

### Configuración de Red
El proyecto está configurado para ejecutarse dentro de una red Docker llamada `iot-net`. Esta red es fundamental para el funcionamiento del sistema ya que permite la comunicación entre diferentes contenedores que son necesarios para el correcto funcionamiento de la aplicación.

### Contenedores en la Red
La red `iot-net` contiene los siguientes contenedores:
- **tareaflask2**: Contenedor principal que ejecuta esta aplicación Flask
- **swag**: Contenedor para manejo de proxy inverso y certificados SSL
- **phpmyadmin**: Interfaz web para administración de la base de datos
- **mariadb**: Servidor de base de datos MySQL

### Importancia de la Red
La utilización de esta red Docker es necesaria porque:
1. Permite la comunicación segura entre contenedores
2. Facilita la integración con otros servicios necesarios
3. Mantiene el aislamiento de la aplicación del resto de la red
4. Permite que los contenedores se comuniquen usando sus nombres como hostnames

### Configuración del Contenedor
Para que la aplicación funcione correctamente, el contenedor `tareaflask2` debe:
1. Estar conectado a la red `iot-net`
2. Tener acceso a los servicios de MariaDB y phpMyAdmin
3. Estar configurado para comunicarse con el proxy inverso (swag)

Esta configuración asegura que todos los componentes necesarios puedan comunicarse entre sí de manera segura y eficiente.