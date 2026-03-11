# Plan de Mejora — `rappi1.py` y `utilidad1.py`

> Fecha: 11 de marzo de 2026  
> Base de referencia: `rappi.py` + `utilidad.py`

---

## 1. Diagnóstico general

El código actual funciona, pero presenta problemas de **seguridad**, **compatibilidad con versiones modernas de librerías**, **robustez ante fallos de red** y **mantenibilidad**. El plan siguiente aborda cada punto de forma concreta.

---

## 2. Mejoras en `utilidad1.py`

### 2.1 Organización del archivo
- [ ] Mover `from pydantic import BaseModel` al bloque de imports del inicio del archivo (PEP 8).
- [ ] Agrupar imports por categoría: stdlib → terceros → internos.

### 2.2 Seguridad y robustez en peticiones HTTP
- [ ] Agregar parámetro `timeout` configurable a `send_api_request` (valor por defecto: `30` segundos) para evitar que las peticiones cuelguen indefinidamente.
- [ ] Cambiar `self.log.debug(error)` por `self.log.warning(error)` donde se traten errores de red, para que sean visibles en el log por defecto.

### 2.3 Configuración del ThreadPool
- [ ] Reemplazar `max_workers=4` hardcodeado por un cálculo dinámico:
  ```python
  THREAD = ThreadPoolExecutor(max_workers=cpu_count() * 2)
  ```

### 2.4 Creación de carpetas
- [ ] Reemplazar `mkdir` por `makedirs(exist_ok=True)` en `crear_carpeta` para evitar errores si el directorio padre no existe.

### 2.5 Decorador `agregar_logger`
- [ ] Reemplazar la llamada a `exit()` por un `raise` de la excepción original para permitir limpieza de recursos y propagación limpia del error.
- [ ] Restaurar los logs de inicio y fin de función (`log.info(f"inicio {nombre_funcion}")`) que estaban comentados, para trazabilidad completa.

### 2.6 Creación del archivo de log
- [ ] Reemplazar `open(ruta_logs, 'a').close()` por `with open(ruta_logs, 'a'): pass` para garantizar el cierre correcto del handle de archivo.

### 2.7 Compatibilidad con Pydantic v2
- [ ] Actualizar `RappiProducto` para usar `model_validate()` en lugar del método `parse_obj()` deprecado.
- [ ] Actualizar el método `.dict()` por `.model_dump()` donde corresponda.

### 2.8 Eliminación de estado global mutable
- [ ] Convertir `RAPPI_HEADER` y `RAPPI_VARIABLES` en funciones que devuelvan copias frescas del diccionario en cada llamada:
  ```python
  def get_rappi_header() -> dict: ...
  def get_rappi_variables() -> dict: ...
  ```
  Esto elimina los efectos secundarios causados por la mutación directa de variables globales entre ejecuciones.

### 2.9 Type hints
- [ ] Agregar type hints en todas las funciones públicas del módulo.

### 2.10 Documentación interna
- [ ] Agregar un comentario explicativo sobre la lógica de `quitar_caracteres_especiales`.

---

## 3. Mejoras en `rappi1.py`

### 3.1 Imports corregidos
- [ ] Agregar `InvalidModificationRequestItemException` a los imports desde `utilidad1` (actualmente se usa pero no se importa, lo que causa `NameError` en tiempo de ejecución).

### 3.2 Seguridad — eliminación de credenciales en código fuente
- [ ] Eliminar la variable `PROXI` con la cookie larga hardcodeada. Las credenciales/cookies no deben vivir en el código fuente. Si se requiere, leerlas desde una variable de entorno o archivo `.env` ignorado por git.

### 3.3 Compatibilidad de `tqdm`
- [ ] Reemplazar `from tqdm.notebook import tqdm` por `from tqdm import tqdm` para funcionar correctamente tanto en scripts CLI como en notebooks.

### 3.4 Uso de copias limpias de configuración
- [ ] Reemplazar el acceso directo a `RAPPI_HEADER` y `RAPPI_VARIABLES` por las nuevas funciones `get_rappi_header()` y `get_rappi_variables()` definidas en `utilidad1`.

### 3.5 Compatibilidad con Pandas ≥ 2.1
- [ ] Reemplazar `.applymap()` (deprecado) por `.map()` en `procesar_data`.

### 3.6 Compatibilidad con Pydantic v2
- [ ] Reemplazar `RappiProducto.parse_obj({...})` por `RappiProducto.model_validate({...})` en `consulta_restaurantes_productos`.

### 3.7 Eliminación de uso innecesario del ThreadPool
- [ ] Reemplazar el bloque `THREAD.submit(get_api_url, store_id)` (que solo ejecuta una lambda trivial) por una list comprehension directa:
  ```python
  self.restaurants = [API_URL_STORES_INFO.format(store_id) for store_id in store_ids]
  ```

### 3.8 Construcción del DataFrame
- [ ] Reemplazar el patrón frágil `DataFrame([product.dict().values() ...])` por:
  ```python
  DataFrame.from_records([product.model_dump() for product in self.products])
  ```
  y renombrar las columnas desde el modelo, en lugar de depender del orden de `.values()`.

### 3.9 Cobertura de errores homogénea
- [ ] Agregar el decorador `@agregar_logger` a `procesar_data` para que los errores se capturen y registren al igual que en `extraer_data`.

### 3.10 Nivel de log correcto para errores HTTP
- [ ] Cambiar `self.log.debug(error)` por `self.log.warning(error)` para los errores `InvalidRequestException` y `NotExecutedRequestException`, aumentando su visibilidad.

### 3.11 Método `transformar_data`
- [ ] Implementar lógica real en `transformar_data`, o —si no aplica— eliminar el método abstracto del ABC y el `pass` en la subclase.

### 3.12 Type hints
- [ ] Agregar type hints a los métodos principales de la clase `Rappi`.

---

## 4. Nuevas herramientas / librerías recomendadas

| Librería | Propósito | Reemplaza |
|---|---|---|
| **`tenacity`** | Reintentos con backoff exponencial y jitter para peticiones HTTP fallidas | Loop `while number_retries <= MAX_RETRIES` manual |
| **`pydantic v2`** (`model_validate`, `model_dump`) | API actualizada para el modelo `RappiProducto` | `parse_obj` y `.dict()` deprecados |
| **`tqdm`** (CLI) | Barra de progreso compatible con scripts y notebooks | `tqdm.notebook` |
| **`python-dotenv`** | Cargar credenciales/cookies desde archivo `.env` | Variables hardcodeadas en el código fuente |
| **`copy.deepcopy`** | Proteger copias de configuración ante mutaciones accidentales | Mutación directa de dicts globales |

---

## 5. Resumen de prioridades

| Prioridad | Item |
|---|---|
| **Alta** | 3.1 Import faltante (`NameError` en ejecución) |
| **Alta** | 3.2 Cookie hardcodeada (riesgo de seguridad) |
| **Alta** | 2.2 Timeout en peticiones HTTP |
| **Alta** | 2.8 Dicts globales mutables |
| **Media** | 3.3 `tqdm.notebook` → `tqdm` |
| **Media** | 3.5 `applymap` deprecado |
| **Media** | 2.7 / 3.6 Pydantic v2 |
| **Media** | 4 Librería `tenacity` para reintentos |
| **Baja** | Type hints, documentación, logs comentados |
