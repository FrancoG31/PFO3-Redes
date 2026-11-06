import json
import queue
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import closing

import bcrypt

HOST = "127.0.0.1"
PORT = 5001
CANTIDAD_WORKERS = 4
DB_PATH = "tareas.db"
MAX_BYTES_PASSWORD = 72
SEGUNDOS_REPORTE = 2

cola_tareas = queue.Queue()

sesiones = {}
sesiones_lock = threading.Lock()


class ErrorPedido(Exception):
    """Excepción personalizada para errores de validación a retornar al cliente."""


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def conectar():
    return closing(sqlite3.connect(DB_PATH, timeout=10))


def init_db():
    with conectar() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT NOT NULL UNIQUE,
                contraseña TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tareas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
                titulo TEXT NOT NULL,
                descripcion TEXT NOT NULL DEFAULT '',
                completada INTEGER NOT NULL DEFAULT 0,
                creada_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()


def agregar_usuario(usuario, contrasenia):
    hashed_pass = bcrypt.hashpw(contrasenia.encode("utf-8"), bcrypt.gensalt())

    with conectar() as conn:
        try:
            conn.execute(
                "INSERT INTO usuarios (usuario, contraseña) VALUES (?, ?)",
                (usuario, hashed_pass),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def verificar_usuario(usuario, contrasenia):
    """Retorna el identificador del usuario si la autenticación es válida, o None."""
    with conectar() as conn:
        result = conn.execute(
            "SELECT id, contraseña FROM usuarios WHERE usuario = ?", (usuario,)
        ).fetchone()

    if not result:
        return None

    usuario_id, hashed_pass = result
    if isinstance(hashed_pass, str):
        hashed_pass = hashed_pass.encode("utf-8")
    if bcrypt.checkpw(contrasenia.encode("utf-8"), hashed_pass):
        return usuario_id
    return None


def listar_tareas_usuario(usuario_id):
    with conectar() as conn:
        filas = conn.execute(
            "SELECT id, titulo, descripcion, completada FROM tareas WHERE usuario_id = ? ORDER BY id",
            (usuario_id,),
        ).fetchall()
    return [
        {"id": id_, "titulo": titulo, "descripcion": descripcion, "completada": bool(completada)}
        for id_, titulo, descripcion, completada in filas
    ]


# ---------------------------------------------------------------------------
# Validaciones
# ---------------------------------------------------------------------------

def campo_texto(datos, campo, obligatorio=True):
    valor = datos.get(campo, "")
    if not isinstance(valor, str) or (obligatorio and not valor.strip()):
        raise ErrorPedido(f"El campo '{campo}' es requerido y debe contener texto")
    return valor.strip()


def campo_entero(datos, campo):
    valor = datos.get(campo)
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise ErrorPedido(f"El campo '{campo}' debe ser de tipo numérico entero")
    return valor


def credenciales(datos):
    usuario = campo_texto(datos, "usuario")
    contrasenia = campo_texto(datos, "contraseña")
    if len(contrasenia.encode("utf-8")) > MAX_BYTES_PASSWORD:
        raise ErrorPedido("La contraseña ingresada supera el límite de longitud permitido")
    return usuario, contrasenia


def usuario_autenticado(pedido):
    """Obtiene la tupla (usuario_id, usuario) correspondiente al token de sesión activo."""
    with sesiones_lock:
        sesion = sesiones.get(pedido.get("token"))
    if not sesion:
        raise ErrorPedido("Sesión no válida o no autenticado")
    return sesion


# ---------------------------------------------------------------------------
# Operaciones
# ---------------------------------------------------------------------------

def op_registro(pedido, datos):
    usuario, contrasenia = credenciales(datos)
    if not agregar_usuario(usuario, contrasenia):
        raise ErrorPedido("El nombre de usuario ya se encuentra registrado")
    return {"mensaje": "Usuario registrado exitosamente"}


def op_login(pedido, datos):
    usuario, contrasenia = credenciales(datos)
    usuario_id = verificar_usuario(usuario, contrasenia)
    if usuario_id is None:
        raise ErrorPedido("Credenciales inválidas")

    token = secrets.token_hex(16)
    with sesiones_lock:
        sesiones[token] = (usuario_id, usuario)
    return {"mensaje": "Inicio de sesión correcto", "token": token}


def op_logout(pedido, datos):
    with sesiones_lock:
        sesiones.pop(pedido.get("token"), None)
    return {"mensaje": "Sesión finalizada"}


def op_crear_tarea(pedido, datos):
    usuario_id, _ = usuario_autenticado(pedido)
    titulo = campo_texto(datos, "titulo")
    descripcion = campo_texto(datos, "descripcion", obligatorio=False)

    with conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO tareas (usuario_id, titulo, descripcion) VALUES (?, ?, ?)",
            (usuario_id, titulo, descripcion),
        )
        conn.commit()
    return {"mensaje": "Tarea creada correctamente", "tarea_id": cursor.lastrowid}


def op_listar_tareas(pedido, datos):
    usuario_id, _ = usuario_autenticado(pedido)
    return {"tareas": listar_tareas_usuario(usuario_id)}


def op_completar_tarea(pedido, datos):
    usuario_id, _ = usuario_autenticado(pedido)
    tarea_id = campo_entero(datos, "tarea_id")

    with conectar() as conn:
        cursor = conn.execute(
            "UPDATE tareas SET completada = 1 WHERE id = ? AND usuario_id = ?",
            (tarea_id, usuario_id),
        )
        conn.commit()
    if cursor.rowcount == 0:
        raise ErrorPedido("No se encontró la tarea especificada")
    return {"mensaje": f"Tarea {tarea_id} completada"}


def op_eliminar_tarea(pedido, datos):
    usuario_id, _ = usuario_autenticado(pedido)
    tarea_id = campo_entero(datos, "tarea_id")

    with conectar() as conn:
        cursor = conn.execute(
            "DELETE FROM tareas WHERE id = ? AND usuario_id = ?",
            (tarea_id, usuario_id),
        )
        conn.commit()
    if cursor.rowcount == 0:
        raise ErrorPedido("No se encontró la tarea especificada")
    return {"mensaje": f"Tarea {tarea_id} eliminada"}


def op_generar_reporte(pedido, datos):
    """Operación de simulación de carga para evidenciar el procesamiento concurrente de los workers."""
    usuario_id, usuario = usuario_autenticado(pedido)
    tareas = listar_tareas_usuario(usuario_id)
    time.sleep(SEGUNDOS_REPORTE)

    completadas = sum(1 for t in tareas if t["completada"])
    return {
        "reporte": {
            "usuario": usuario,
            "total": len(tareas),
            "completadas": completadas,
            "pendientes": len(tareas) - completadas,
        }
    }


OPERACIONES = {
    "registro": op_registro,
    "login": op_login,
    "logout": op_logout,
    "crear_tarea": op_crear_tarea,
    "listar_tareas": op_listar_tareas,
    "completar_tarea": op_completar_tarea,
    "eliminar_tarea": op_eliminar_tarea,
    "generar_reporte": op_generar_reporte,
}


def procesar(pedido):
    operacion = OPERACIONES.get(pedido.get("tipo"))
    if operacion is None:
        raise ErrorPedido(f"Tipo de tarea no reconocido: {pedido.get('tipo')!r}")

    datos = pedido.get("datos") or {}
    if not isinstance(datos, dict):
        raise ErrorPedido("El campo 'datos' debe estructurarse como un objeto JSON")
    return operacion(pedido, datos)


# ---------------------------------------------------------------------------
# Red y workers
# ---------------------------------------------------------------------------

class Conexion:
    """Encapsula el socket del cliente con un cerrojo (lock) para evitar la superposición de mensajes entre workers."""

    def __init__(self, sock, direccion):
        self.sock = sock
        self.direccion = direccion
        self.lock = threading.Lock()

    def enviar(self, mensaje):
        linea = (json.dumps(mensaje, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.sock.sendall(linea)
            except OSError:
                print(f"[!] No se pudo enviar respuesta a {self.direccion}: el cliente cerró la conexión")


def worker(nombre):
    while True:
        conexion, pedido = cola_tareas.get()
        tipo = pedido.get("tipo")
        print(f"[{nombre}] procesando '{tipo}' (id={pedido.get('id')}) para {conexion.direccion}")

        try:
            respuesta = {"estado": "ok", **procesar(pedido)}
        except ErrorPedido as error:
            respuesta = {"estado": "error", "error": str(error)}
        except Exception as error:
            print(f"[{nombre}] error no previsto: {error!r}")
            respuesta = {"estado": "error", "error": "Error interno del servidor"}

        respuesta["id"] = pedido.get("id")
        respuesta["worker"] = nombre
        conexion.enviar(respuesta)
        cola_tareas.task_done()


def atender_cliente(sock, direccion):
    conexion = Conexion(sock, direccion)
    print(f"[+] Conexión entrante aceptada: {direccion}")

    try:
        with sock, sock.makefile("r", encoding="utf-8") as lector:
            for linea in lector:
                if not linea.strip():
                    continue
                try:
                    pedido = json.loads(linea)
                    if not isinstance(pedido, dict):
                        raise ValueError
                except ValueError:
                    conexion.enviar({"id": None, "estado": "error", "error": "Estructura JSON no válida"})
                    continue
                cola_tareas.put((conexion, pedido))
    except (OSError, UnicodeDecodeError):
        pass
    finally:
        print(f"[-] Conexión cerrada por el cliente: {direccion}")


def main():
    init_db()

    for i in range(1, CANTIDAD_WORKERS + 1):
        threading.Thread(target=worker, args=(f"worker-{i}",), daemon=True).start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as servidor:
        servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        servidor.bind((HOST, PORT))
        servidor.listen()
        servidor.settimeout(1)  # Facilita la captura de interrupciones (Ctrl+C) en Windows
        print(f"Servidor iniciado y escuchando en {HOST}:{PORT} con {CANTIDAD_WORKERS} workers")

        try:
            while True:
                try:
                    sock, direccion = servidor.accept()
                except socket.timeout:
                    continue
                sock.settimeout(None)
                threading.Thread(target=atender_cliente, args=(sock, direccion), daemon=True).start()
        except KeyboardInterrupt:
            print("\nServidor finalizado por el usuario")


if __name__ == "__main__":
    main()
