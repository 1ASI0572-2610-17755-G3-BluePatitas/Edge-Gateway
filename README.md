# Bluepatitas - Edge Gateway

Este directorio contiene el código del **Edge Gateway** para el proyecto Bluepatitas. El Edge Gateway es una aplicación intermediaria ligera construida en Python (con Flask) que se encarga de recibir datos (telemetría y ubicación) desde los dispositivos físicos (ej. collares GPS, ESP32) y sincronizarlos con el servidor principal (Spring Boot) en la nube.

Además de sincronizar, realiza validaciones locales rápidas (Edge Computing), como la evaluación de geocercas y la gestión de la cola del dispensador de comida.

## 📋 Requisitos Previos

Para ejecutar el Edge Gateway en cualquier computadora (o placa como una Raspberry Pi), debes asegurarte de tener:

1. **Python 3.8 o superior** instalado en el sistema.
2. **Pip** (el gestor de paquetes de Python) para instalar las dependencias.

## 🚀 Inicialización y Uso

### 1. Instalar Dependencias

Abre una terminal en esta misma carpeta (`edge`) y ejecuta el siguiente comando para instalar las librerías necesarias:

```bash
pip install flask flask-cors requests
```

### 2. Base de Datos Local (`edge_data.db`)

No necesitas configurar ni crear ninguna base de datos manualmente. 
Al iniciar la aplicación, esta detectará si existe el archivo de caché local. Si no existe, **generará de forma automática** el archivo `edge_data.db` con las tablas `lecturas` y `ubicaciones` que se usan para almacenar los datos temporalmente en caso de no tener internet.

### 3. Variables de Entorno (Opcional)

El sistema intentará conectarse por defecto a tu entorno en producción. Si necesitas probar conectándolo a tu backend en local u otra dirección, puedes establecer la variable de entorno `BACKEND_URL` antes de ejecutar. 

Ejemplo (en Windows PowerShell):
```powershell
$env:BACKEND_URL="http://localhost:8080"
```
Ejemplo (en Linux/Mac):
```bash
export BACKEND_URL="http://localhost:8080"
```

### 4. Ejecutar el Servidor Edge

Para levantar el servidor, ejecuta el script principal:

```bash
python edge_gateway.py
```

El servidor iniciará en el puerto **18090**. A partir de este momento, tus dispositivos ESP32 ya podrán enviarle peticiones POST a `http://<IP-DE-ESTA-PC>:18090/telemetria` y `http://<IP-DE-ESTA-PC>:18090/location`.

---

## 🛠️ Herramientas Auxiliares (Testing)

En esta carpeta también encontrarás dos archivos adicionales:

*   **`debug_tool.py`**
*   **`simulador_estatico.py`**

**IMPORTANTE:** Ninguno de estos archivos es necesario para que el `edge_gateway.py` funcione en producción. Son herramientas creadas exclusivamente para realizar pruebas de desarrollo, simular movimiento de las mascotas sin necesidad del hardware real y forzar el disparo de alertas de geocerca.
