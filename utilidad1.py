# ──────────────────────────────────────────────────────────────────────────────
# stdlib
# ──────────────────────────────────────────────────────────────────────────────
from abc import ABC, abstractmethod
from datetime import datetime
from logging import Formatter, getLogger, StreamHandler
from logging.handlers import TimedRotatingFileHandler
from os import getenv, makedirs
from os.path import abspath, dirname, exists, join
from pathlib import Path
from sys import stdout
from traceback import TracebackException
from unicodedata import normalize
import re

# ──────────────────────────────────────────────────────────────────────────────
# terceros
# ──────────────────────────────────────────────────────────────────────────────
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count

from pydantic import BaseModel
from requests.exceptions import JSONDecodeError


# ──────────────────────────────────────────────────────────────────────────────
# Excepciones personalizadas
# ──────────────────────────────────────────────────────────────────────────────

class RappiException(Exception):
    """Excepción base del scraper Rappi."""

    def __init__(self, msg: str = "") -> None:
        super().__init__()
        self.msg = msg

    def __str__(self) -> str:
        return f" {self.__class__.__name__}: {self.msg}\n"


class InvalidRequestException(RappiException):
    """Se lanza cuando una petición HTTP devuelve una respuesta inesperada."""

    def __init__(self, url: str, method: str, status_code: int, status_name: str, reason: str | None = None) -> None:
        self.url = url
        self.status_code = status_code
        message = (
            f"The Request {url} using the {method} method failed "
            f"with Status Code {status_code} ({status_name})"
        )
        if reason is not None:
            message += f" and invalid response ({reason})."
        super().__init__(message)


class NotExecutedRequestException(RappiException):
    """Se lanza cuando una petición HTTP no pudo ejecutarse."""

    def __init__(self, url: str, exception: Exception, reason: str | None = None) -> None:
        super().__init__(
            f"The Request {url} wasn't executed successfully "
            f"due to an unexpected exception ({exception})"
        )


class InvalidModificationRequestItemException(RappiException):
    """Se lanza cuando no se puede modificar un elemento del Request (header/payload)."""

    def __init__(self, item_name: str, error: Exception, stacktrace=None) -> None:
        super().__init__(
            f"The Request's {item_name} wasn't modified successfully "
            f"due to '{error.__class__.__name__}' error with the message: '{error}'"
        )


class RestaurantsNotFoundException(RappiException):
    """Se lanza cuando el scraper no logra extraer ningún restaurante."""


# ──────────────────────────────────────────────────────────────────────────────
# Modelo de datos
# ──────────────────────────────────────────────────────────────────────────────

class RappiProducto(BaseModel):
    """Estructura de un producto de Rappi."""

    popular: bool = False
    producto: str = ""
    descripcion: str = ""
    precio_descuento: float = 0.00
    precio: float = 0.00
    restaurante: str = ""
    estado: str = ""
    categoria: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Constantes y configuración
# ──────────────────────────────────────────────────────────────────────────────

# Pool de hilos con tamaño dinámico según los cores disponibles
THREAD = ThreadPoolExecutor(max_workers=cpu_count() * 2)

# Máximo de reintentos para peticiones fallidas
MAX_RETRIES: int = 3

# URLs de la API de Rappi
API_URL_GUEST            = "https://services.rappi.pe/api/rocket/v2/guest"
API_URL_GUEST_PASSPORT   = "https://services.rappi.pe/api/rocket/v2/guest/passport/"
API_URL_STORES_CATALOG   = "https://services.rappi.pe/api/web-gateway/web/restaurants-bus/stores/catalog-paged/home"
API_URL_STORES_INFO      = "https://services.rappi.pe/api/web-gateway/web/restaurants-bus/store/id/{0}/"

# Tiempo máximo de espera por petición (segundos)
REQUEST_TIMEOUT: int = 30
USE_ENV_PROXIES: bool = getenv("RAPPI_USE_ENV_PROXIES", "0") == "1"
VERIFY_SSL: bool = getenv("RAPPI_VERIFY_SSL", "1") == "1"


def get_rappi_header() -> dict:
    """
    Devuelve una copia fresca del header inicial de Rappi.

    Usar esta función en lugar de un dict global mutable evita efectos
    secundarios entre ejecuciones consecutivas.
    """
    return {
        "accept": "application/json",
        "accept-language": "es-PE",
        "access-control-allow-headers": "*",
        "access-control-allow-origin": "*",
        "app-version": "e1de6be43aa29091011474615d7ac0810051c36a",
        "deviceid": "958340a2-7d66-4f2a-b032-575552e0a160",
        "needappsflyerid": "false",
        "sec-ch-ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Microsoft Edge";v="114"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "x-application-id": "rappi-microfront-web/e1de6be43aa29091011474615d7ac0810051c36a",
        "Referer": "https://www.rappi.com.pe/",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36 Edg/114.0.1823.82"
        ),
    }


def get_rappi_variables() -> dict:
    """
    Devuelve una copia fresca del payload inicial de Rappi.

    Usar esta función en lugar de un dict global mutable evita efectos
    secundarios entre ejecuciones consecutivas.
    """
    return {
        "lat": -12.145395,
        "lng": -77.021936,
        "store_type": "restaurant",
        "is_prime": "false",
        "states": ["opened", "unavailable", "closed"],
        "prime_config": {"unlimited_shipping": "false"},
    }


ENUM_RESTAURANT_STATUS: dict[str, str] = {
    "CLOSED": "Restaurante cerrado",
    "OFF": "Restaurante cerrado",
    "ONLY_PICKUP_AVAILABLE": "",
    "OPEN": "Restaurante abierto",
    "OUT_OF_COVERAGE": "Restaurante abierto",
    "TEMPORARILY_UNAVAILABLE": "Temporalmente no disponible",
    "TEMPORARILY_UNAVAILABLE_OFF": "Temporalmente no disponible",
}


# ──────────────────────────────────────────────────────────────────────────────
# Funciones utilitarias
# ──────────────────────────────────────────────────────────────────────────────

def print_error_detail(error: Exception) -> None:
    """Imprime por consola el detalle completo de una excepción."""
    exception = TracebackException.from_exception(error)
    print("Ha ocurrido un error:")
    for line in exception.format(chain=True):
        print(line)


# El header mutable se pasa como argumento para que send_api_request no dependa
# de estado global.  El caller es responsable de proveerlo.
def send_api_request(
    url_request: str,
    request_function,
    request_params: dict | None = None,
    header: dict | None = None,
) -> dict:
    """
    Envía una petición HTTP y devuelve la respuesta en formato JSON.

    Parameters
    ----------
    url_request : str
        URL de destino.
    request_function : callable
        Función de requests a invocar (p. ej. requests.get, requests.post).
    request_params : dict, optional
        Parámetros adicionales para la función de requests.
    header : dict, optional
        Header HTTP. Si es None se usa get_rappi_header().

    Returns
    -------
    dict
        Respuesta JSON de la API.

    Raises
    ------
    NotExecutedRequestException
        Si la petición no pudo ejecutarse.
    InvalidRequestException
        Si la respuesta tiene un código de estado fuera del rango 2xx.
    """
    if request_params is None:
        request_params = {}
    else:
        request_params = request_params.copy()
    if header is None:
        header = get_rappi_header()

    bound_session = getattr(request_function, "__self__", None)
    if bound_session is not None and hasattr(bound_session, "trust_env"):
        bound_session.trust_env = USE_ENV_PROXIES

    if not USE_ENV_PROXIES:
        request_params.setdefault("proxies", {"http": None, "https": None})

    if "verify" not in request_params:
        request_params["verify"] = VERIFY_SSL

    try:
        response = request_function(
            url_request,
            headers=header,
            timeout=REQUEST_TIMEOUT,
            **request_params,
        )
    except Exception as error:
        raise NotExecutedRequestException(url_request, error)

    status_code = response.status_code
    if 200 <= status_code <= 299:
        try:
            response_text = response.json()
            if len(response_text) > 0:
                return response_text
            reason = "Empty Response"
        except JSONDecodeError:
            reason = "Invalid Response Format"
    else:
        reason = None

    raise InvalidRequestException(
        url_request, response.request.method, status_code, response.reason, reason
    )


def crear_carpeta(ruta: str) -> None:
    """Crea la carpeta indicada (y sus padres) si no existe."""
    makedirs(ruta, exist_ok=True)


def obtener_logger(nombre: str, ruta: str | None = None, max_dias: int = 14):
    """
    Obtiene un logger con el nombre indicado.

    Parameters
    ----------
    nombre : str
        Nombre del logger.
    ruta : str or None, default None
        Carpeta donde se almacenan los archivos de log.
    max_dias : int, default 14
        Días máximos que se conservan los archivos de log.

    Returns
    -------
    logging.Logger
    """
    logger = getLogger(nombre)
    if logger.hasHandlers():
        return logger

    logger.setLevel(10)  # DEBUG

    log_format = "%(asctime)s %(levelname)s: %(message)s [en %(filename)s:%(funcName)s %(lineno)d]"

    # Handler de consola (INFO+)
    stream_handler = StreamHandler(stdout)
    stream_handler.setLevel(20)  # INFO
    stream_handler.setFormatter(Formatter(log_format))
    logger.addHandler(stream_handler)

    # Handler de archivo rotatorio por día
    ruta_logs = str(Path(ruta or Path.cwd(), f"{nombre}.log"))
    makedirs(dirname(ruta_logs), exist_ok=True)
    if not exists(ruta_logs):
        with open(ruta_logs, "a"):
            pass

    file_handler = TimedRotatingFileHandler(
        ruta_logs, when="midnight", backupCount=max_dias, encoding="utf-8"
    )
    file_handler.setLevel(10)  # DEBUG
    file_handler.setFormatter(Formatter(log_format))
    logger.addHandler(file_handler)

    return logger


def agregar_logger(funcion):
    """
    Decorador que registra el inicio y fin de la función decorada.
    En caso de excepción la registra como ERROR y la re-lanza para permitir
    limpieza de recursos en la cadena de llamadas.
    """
    def wrapper(*args, **kwargs):
        nombre_funcion = funcion.__name__
        args[0].log.info(f"Inicio: {nombre_funcion}")
        try:
            resultado = funcion(*args, **kwargs)
            args[0].log.info(f"Fin: {nombre_funcion}")
            return resultado
        except Exception as e:
            args[0].log.error(f"Error en {nombre_funcion}: {e}")
            raise

    return wrapper


def quitar_caracteres_especiales(text: str) -> str:
    """
    Elimina marcas diacríticas y caracteres especiales de un texto,
    conservando la ñ y las letras acentuadas propias del español.

    El proceso es:
    1. Descomponer el texto en forma NFD (separando letra base + diacrítico).
    2. Eliminar las marcas de combinación (\u0300-\u036f) excepto la ñ.
    3. Recomponer en forma NFC.
    """
    return normalize(
        "NFC",
        re.sub(
            r"([^n\u0300-\u036f]|n(?!\u0303(?![\u0300-\u036f])))[\u0300-\u036f';\¿\?\!\,\(\)\*\®\"\|\r\n]+",
            r"\1",
            normalize("NFD", text),
            0,
            re.I,
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Clase base abstracta
# ──────────────────────────────────────────────────────────────────────────────

class Base(ABC):
    def __init__(self) -> None:
        self.python_ubicacion = abspath(dirname(__file__))
        self.ruta_data = join(self.python_ubicacion, "data")
        self.ruta_logs = join(self.python_ubicacion, "logs")
        crear_carpeta(self.ruta_data)
        self.hoy = datetime.today()
        self.log = obtener_logger(self.__class__.__name__, self.ruta_logs)

    @abstractmethod
    def navegar(self) -> None:
        """Orquesta el flujo completo del scraper."""
        pass

    @abstractmethod
    def extraer_data(self) -> None:
        """Extrae los datos crudos de la fuente."""
        pass

    @abstractmethod
    def procesar_data(self) -> None:
        """Limpia y transforma los datos extraídos."""
        pass

    @abstractmethod
    def transformar_data(self) -> None:
        """Aplica transformaciones adicionales al DataFrame."""
        pass

    @abstractmethod
    def exportar_data(self) -> None:
        """Exporta el DataFrame procesado a un archivo."""
        pass


if __name__ == "__main__":
    pass
