# Explicacion del codigo: utilidad.py y rappi.py

Este documento resume como funciona el scraper del proyecto, explicando responsabilidades, flujo y piezas clave de los modulos `utilidad.py` y `rappi.py`.

## Vision general

El proyecto implementa un scraper de restaurantes/productos de Rappi con este flujo:

1. Preparar entorno (carpetas, fecha, logger).
2. Autenticarse y construir solicitudes a APIs de Rappi.
3. Obtener restaurantes.
4. Obtener productos de cada restaurante en paralelo.
5. Limpiar y normalizar datos.
6. Exportar a CSV.

---

## utilidad.py

`utilidad.py` contiene la infraestructura comun del proyecto: excepciones, configuraciones de API, modelo de datos, utilidades HTTP, logging y clase base abstracta.

### 1) Excepciones personalizadas

- `RappiException`: excepcion base del proyecto.
- `InvalidRequestException`: se lanza cuando una request responde con codigo inesperado o contenido invalido.
- `NotExecutedRequestException`: se lanza cuando la request ni siquiera pudo ejecutarse (error de red, etc.).
- `InvalidModificationRequestItemException`: error al modificar headers/payload esperados.
- `RestaurantsNotFoundException`: no se encontraron restaurantes para continuar el proceso.

Estas excepciones mejoran trazabilidad y control de errores con mensajes mas claros.

### 2) Modelo de datos

- `RappiProducto` (Pydantic `BaseModel`): define la estructura de cada producto.
- Campos: popular, producto, descripcion, precio_descuento, precio, restaurante, estado, categoria.

Ventaja: valida/estandariza los datos de entrada antes de cargarlos al DataFrame.

### 3) Configuracion global

- `THREAD`: `ThreadPoolExecutor(max_workers=4)` para paralelismo.
- `MAX_RETRIES`: cantidad maxima de reintentos para requests fallidas.
- Constantes API:
  - `API_URL_GUEST`
  - `API_URL_GUEST_PASSPORT`
  - `API_URL_STORES_CATALOG`
  - `API_URL_STORES_INFO`
- `RAPPI_HEADER`: headers base para requests HTTP.
- `RAPPI_VARIABLES`: payload base para catalogo (lat/lng, tipo tienda, estados, etc.).
- `ENUM_RESTAURANT_STATUS`: mapeo de estados de API a textos legibles.

### 4) Funciones utilitarias

- `print_error_detail(error)`: imprime traceback detallado.
- `send_api_request(url_request, request_function, request_params={})`:
  - Ejecuta request con headers globales.
  - Valida status code 2xx.
  - Intenta parsear JSON.
  - Si falla, lanza excepciones del dominio (`InvalidRequestException`/`NotExecutedRequestException`).
- `crear_carpeta(ruta)`: crea carpeta si no existe.
- `obtener_logger(...)`: configura logger con:
  - salida a consola (INFO)
  - archivo rotatorio diario (DEBUG)
- `agregar_logger(funcion)`:
  - decorador que ejecuta la funcion.
  - ante excepcion: registra en log y finaliza proceso con `exit()`.
- `quitar_caracteres_especiales(text)`: normaliza y limpia caracteres especiales/diacriticos.

### 5) Clase base abstracta

- `Base(ABC)` define estructura comun para scrapers.
- En `__init__`:
  - calcula rutas de trabajo (`data`, `logs`)
  - crea carpeta `data`
  - define fecha actual (`self.hoy`)
  - inicializa logger de la clase hija
- Metodos abstractos obligatorios:
  - `navegar`
  - `extraer_data`
  - `procesar_data`
  - `transformar_data`
  - `exportar_data`

Esto obliga a cualquier implementacion concreta (como `Rappi`) a respetar el ciclo ETL.

---

## rappi.py

`rappi.py` implementa la logica concreta del scraper usando la infraestructura de `utilidad.py`.

### 1) Clase Rappi

`Rappi` hereda de `Base`.

En su constructor:

- inicializa lista de restaurantes (`self.restaurants`)
- inicializa lista de productos (`self.products`)
- inicializa DataFrame (`self.data`)
- lista para enlaces a reintentar (`self.links_to_retry`)

### 2) Metodo orquestador: `navegar()`

Ejecuta el pipeline completo:

1. `consulta_restaurantes()`
2. `extraer_data()`
3. `procesar_data()`
4. `exportar_data()`
5. `transformar_data()`

Tambien registra hora de inicio/fin y duracion total en log.

### 3) Obtencion de restaurantes: `consulta_restaurantes()`

Responsabilidades:

- Autenticacion inicial (guest token) para obtener `authorization` y ajustar headers.
- Limpieza/modificacion de `RAPPI_HEADER` para la secuencia de requests posterior.
- Consulta al catalogo de tiendas (`API_URL_STORES_CATALOG`) para obtener `store_ids`.
- Construccion de URLs de detalle por restaurante (`API_URL_STORES_INFO.format(id)`) en paralelo.
- Guarda URLs en `self.restaurants`.
- Ajusta payload (`RAPPI_VARIABLES`) quitando `states` para requests siguientes.

Si no obtiene restaurantes, lanza `RestaurantsNotFoundException`.

### 4) Extraccion principal: `extraer_data()`

Tiene decorador `@agregar_logger`.

Flujo:

- Verifica que existan restaurantes.
- Ejecuta requests en paralelo con `THREAD.submit(...)` y `requests.Session()`.
- Implementa reintentos hasta `MAX_RETRIES` para codigos especificos (400, 429, 500).
- Acumula respuestas exitosas en `restaurants_info`.
- Para cada restaurante extraido, llama en paralelo a `consulta_restaurantes_productos(...)`.
- Consolida todos los productos en `self.products`.
- Convierte productos a `DataFrame` con columnas finales:
  - Popular
  - Producto
  - Descripcion
  - Precio con descuento
  - Precio sin descuento
  - Restaurante
  - Disponible
  - Categoria

### 5) Parseo de productos: `consulta_restaurantes_productos(restaurant_data)`

Toma un JSON de restaurante y devuelve lista de `RappiProducto`:

- Lee nombre de restaurante, tags/categoria y estado.
- Recorre corredores/categorias de menu (`corridors`) y sus productos.
- Para cada producto, arma objeto tipado con campos normalizados.

### 6) Apoyo de descuentos: `get_discounts(item)`

- Si hay descuentos, toma el `price` del primero.
- Si no hay, devuelve `np.nan`.

### 7) Limpieza de datos: `procesar_data()`

Transformaciones principales del DataFrame:

- Agrega columna `Fecha`.
- Ordena por restaurante/producto/descripcion/popularidad.
- Elimina duplicados por campos clave.
- Reemplaza saltos de linea.
- Convierte y formatea precios a 2 decimales y con formato local (coma decimal).
- Reemplaza `nan` por vacio en precios.
- Limpia sufijos en nombres de restaurante via regex.
- Traduce `Popular` de booleano textual a etiqueta (`popular` o vacio).
- Mapea estado de disponibilidad con `ENUM_RESTAURANT_STATUS`.

Si ocurre error, registra detalle y traza.

### 8) Exportacion: `exportar_data()`

- Si el DataFrame no esta vacio, genera nombre:
  - `Rappi_YYYY-MM-DD_N.csv`
- Guarda CSV en carpeta `data` con:
  - separador `;`
  - encoding `utf-8-sig`
- Registra resultado en log.

### 9) Transformacion final: `transformar_data()`

Actualmente esta como `pass` (pendiente de implementacion).

### 10) Punto de entrada

En bloque `if __name__ == '__main__':`

- instancia `Rappi`
- ejecuta `rappi.navegar()`

---

## Relacion entre ambos archivos

- `utilidad.py` aporta base tecnica reusable (framework interno del scraper).
- `rappi.py` implementa la logica de negocio concreta de extraccion/procesamiento de Rappi.

En terminos de arquitectura:

- `Base` define el contrato.
- `Rappi` lo implementa.
- `send_api_request` centraliza llamadas HTTP y manejo de errores.
- `RappiProducto` garantiza estructura consistente antes de pasar a pandas.

---

## Observaciones tecnicas utiles

- Hay mutacion de estructuras globales (`RAPPI_HEADER`, `RAPPI_VARIABLES`) dentro de metodos; en ejecuciones repetidas puede generar efectos secundarios.
- El decorador `agregar_logger` usa `exit()` ante error, lo que corta ejecucion completa (incluyendo posibles procesos llamadores).
- `transformar_data` aun no aporta logica.
- El valor `PROXI` se define pero no se usa de forma efectiva en la request activa (linea comentada).

Estas observaciones no impiden entender el flujo, pero son puntos a considerar para mantenimiento.
