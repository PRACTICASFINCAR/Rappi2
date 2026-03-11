from concurrent import futures
from concurrent.futures import wait
from copy import copy
from datetime import datetime
from os import getenv
from os.path import join
from urllib.parse import unquote, urlparse

import numpy as np
import pandas as pd
import requests
from pandas import DataFrame
from tqdm import tqdm

from utilidad1 import (
    API_URL_GUEST,
    API_URL_STORES_CATALOG,
    API_URL_STORES_INFO,
    Base,
    ENUM_RESTAURANT_STATUS,
    InvalidModificationRequestItemException,
    InvalidRequestException,
    MAX_RETRIES,
    NotExecutedRequestException,
    RappiProducto,
    RestaurantsNotFoundException,
    THREAD,
    USE_ENV_PROXIES,
    VERIFY_SSL,
    agregar_logger,
    get_rappi_header,
    get_rappi_variables,
    print_error_detail,
    send_api_request,
)


class Rappi(Base):
    def __init__(self) -> None:
        super().__init__()
        self.restaurants: list[str] = []
        self.products: list[RappiProducto] = []
        self.data: DataFrame = DataFrame()
        self.links_to_retry: list[str] = []
        self.rappi_header: dict = get_rappi_header()
        self.rappi_variables: dict = get_rappi_variables()
        self._playwright_driver = None
        self._playwright_request_context = None

    def navegar(self) -> None:
        inicio = datetime.now()
        self.log.info("Start")
        if self.is_full_playwright_mode:
            self.log.info("Playwright full mode enabled (all API calls through Playwright)")
            self._open_playwright_context()
        if self.is_hybrid_mode:
            self.log.info("Hybrid mode enabled (Playwright bootstrap + requests bulk extraction)")
        try:
            self.consulta_restaurantes()
            self.extraer_data()
            self.procesar_data()
            self.exportar_data()
            self.transformar_data()
            fin = datetime.now()
            duracion = fin - inicio
            self.log.info(f"Duration: {duracion}")
        finally:
            if self.is_full_playwright_mode:
                self._close_playwright_context()

    @property
    def is_hybrid_mode(self) -> bool:
        return getenv("RAPPI_HYBRID_MODE", "0") == "1"

    @property
    def is_full_playwright_mode(self) -> bool:
        return getenv("RAPPI_PLAYWRIGHT_FULL", "0") == "1"

    @property
    def use_playwright_bootstrap(self) -> bool:
        return self.is_hybrid_mode or self.is_full_playwright_mode

    def _build_playwright_proxy(self) -> dict | None:
        if not USE_ENV_PROXIES:
            return None

        proxy_url = getenv("HTTPS_PROXY") or getenv("HTTP_PROXY")
        if not proxy_url:
            return None

        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            return None

        proxy_config: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
        if parsed.username:
            proxy_config["username"] = unquote(parsed.username)
        if parsed.password:
            proxy_config["password"] = unquote(parsed.password)
        return proxy_config

    def _send_api_request_playwright(self, url_request: str, payload: dict | None = None) -> dict:
        try:
            if self._playwright_request_context is not None:
                request_context = self._playwright_request_context
                response = request_context.post(url_request, data=payload, headers=self.rappi_header)
            else:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    request_context = playwright.request.new_context(
                        ignore_https_errors=not VERIFY_SSL,
                        extra_http_headers=self.rappi_header,
                        proxy=self._build_playwright_proxy(),
                    )
                    response = request_context.post(url_request, data=payload)
                    request_context.dispose()

            status_code = response.status
            if 200 <= status_code <= 299:
                response_text = response.json()
                if len(response_text) > 0:
                    return response_text
                raise InvalidRequestException(
                    url_request,
                    "POST",
                    status_code,
                    response.status_text,
                    "Empty Response",
                )

            raise InvalidRequestException(
                url_request,
                "POST",
                status_code,
                response.status_text,
            )
        except InvalidRequestException:
            raise
        except Exception as error:
            error_text = str(error).lower()
            ssl_fallback_enabled = getenv("RAPPI_PLAYWRIGHT_SSL_FALLBACK", "1") == "1"
            certificate_error = "unable to verify the first certificate" in error_text

            if certificate_error and ssl_fallback_enabled:
                self.log.warning(
                    "Playwright SSL verification failed; retrying request with ignore_https_errors=True"
                )
                try:
                    from playwright.sync_api import sync_playwright

                    with sync_playwright() as playwright:
                        request_context = playwright.request.new_context(
                            ignore_https_errors=True,
                            extra_http_headers=self.rappi_header,
                            proxy=self._build_playwright_proxy(),
                        )
                        response = request_context.post(url_request, data=payload)
                        status_code = response.status
                        if 200 <= status_code <= 299:
                            response_text = response.json()
                            if len(response_text) > 0:
                                request_context.dispose()
                                return response_text
                            request_context.dispose()
                            raise InvalidRequestException(
                                url_request,
                                "POST",
                                status_code,
                                response.status_text,
                                "Empty Response",
                            )

                        request_context.dispose()
                        raise InvalidRequestException(
                            url_request,
                            "POST",
                            status_code,
                            response.status_text,
                        )
                except InvalidRequestException:
                    raise
                except Exception as ssl_fallback_error:
                    raise NotExecutedRequestException(url_request, ssl_fallback_error)

            raise NotExecutedRequestException(url_request, error)

    def _open_playwright_context(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as error:
            raise NotExecutedRequestException(
                "playwright.init",
                Exception(
                    "Playwright no disponible. Instala con 'pip install playwright' y luego 'playwright install chromium'."
                ),
            ) from error

        self._playwright_driver = sync_playwright().start()
        self._playwright_request_context = self._playwright_driver.request.new_context(
            ignore_https_errors=not VERIFY_SSL,
            extra_http_headers=self.rappi_header,
            proxy=self._build_playwright_proxy(),
        )

    def _close_playwright_context(self) -> None:
        if self._playwright_request_context is not None:
            self._playwright_request_context.dispose()
            self._playwright_request_context = None
        if self._playwright_driver is not None:
            self._playwright_driver.stop()
            self._playwright_driver = None

    @agregar_logger
    def extraer_data(self) -> None:
        self.log.info("Extracting all products offered by the restaurants in Rappi")
        if len(self.restaurants) <= 0:
            raise RestaurantsNotFoundException(
                "The crawler couldn't extract any restaurants, so the process can't continue"
            )

        number_retries = 1
        restaurants = copy(self.restaurants)
        links_to_go: list[str] = []
        restaurants_info: list[dict] = []

        while (number_retries <= MAX_RETRIES) and (len(restaurants) > 0):
            self.log.info("------------------------------")
            self.log.info(f"TRY N° {number_retries}")
            progress = tqdm(total=len(restaurants), desc="Extracting data")
            if self.is_full_playwright_mode:
                for restaurant in restaurants:
                    try:
                        restaurant_info = self._send_api_request_playwright(
                            restaurant,
                            payload=self.rappi_variables,
                        )
                        restaurants_info.append(restaurant_info)
                    except InvalidRequestException as error:
                        if error.status_code in [400, 429, 500]:
                            links_to_go.append(error.url)
                        self.log.warning(error)
                    except NotExecutedRequestException as error:
                        self.log.warning(error)
                    progress.update(1)
                progress.close()
            else:
                session = requests.Session()
                session.trust_env = False
                future_restaurants = [
                    THREAD.submit(
                        send_api_request,
                        restaurant,
                        session.post,
                        {"json": self.rappi_variables},
                        self.rappi_header,
                    )
                    for restaurant in restaurants
                ]

                for future_restaurant in futures.wait(future_restaurants).done:
                    try:
                        restaurants_info.append(future_restaurant.result())
                    except InvalidRequestException as error:
                        if error.status_code in [400, 429, 500]:
                            links_to_go.append(error.url)
                        self.log.warning(error)
                    except NotExecutedRequestException as error:
                        self.log.warning(error)
                    progress.update(1)

                progress.close()
                session.close()
            restaurants = copy(links_to_go)
            links_to_go = []
            number_retries += 1
            self.log.info(f"Number of extracted restaurants: {len(restaurants_info)}")
            self.log.info(f"Number of remaining restaurants: {len(restaurants)}")

        self.log.info(f"Number of all extracted restaurants: {len(restaurants_info)}")
        self.log.info(f"Number of total tryings: {number_retries - 1}")

        progress = tqdm(total=len(restaurants_info), desc="Extracting products")
        future_products = [
            THREAD.submit(self.consulta_restaurantes_productos, restaurant_info)
            for restaurant_info in restaurants_info
        ]
        for future_product in futures.wait(future_products).done:
            self.products.extend(future_product.result())
            progress.update(1)
        progress.close()

        product_records = [product.model_dump() for product in self.products]
        self.data = DataFrame.from_records(product_records)
        if not self.data.empty:
            self.data.rename(
                columns={
                    "popular": "Popular",
                    "producto": "Producto",
                    "descripcion": "Descripcion",
                    "precio_descuento": "Precio con descuento",
                    "precio": "Precio sin descuento",
                    "restaurante": "Restaurante",
                    "estado": "Disponible",
                    "categoria": "Categoria",
                },
                inplace=True,
            )

        self.log.info(
            f"Number of record before the data cleaning process: {len(self.data)}"
        )
        self.log.info("The data has been extracted successfully")

    @agregar_logger
    def procesar_data(self) -> None:
        try:
            self.log.info("Cleaning the data extracted by the scraper")
            self.data["Fecha"] = self.hoy.strftime("%Y-%m-%d")
            self.data.sort_values(
                ["Restaurante", "Producto", "Descripcion", "Popular"],
                inplace=True,
                ascending=[True, True, True, False],
            )
            self.data.drop_duplicates(
                [
                    "Restaurante",
                    "Producto",
                    "Descripcion",
                    "Precio con descuento",
                    "Precio sin descuento",
                ],
                keep="first",
                inplace=True,
            )
            self.data.replace({"\n": " "}, inplace=True)
            self.data = self.data.astype({"Popular": str})
            self.data["Precio con descuento"] = self.data[
                "Precio con descuento"
            ].apply(lambda x: round(x, 2) if pd.notna(x) else x)
            self.data["Precio con descuento"] = self.data[
                "Precio con descuento"
            ].map(lambda value: "" if pd.isna(value) else "{:,.2f}".format(value))
            self.data["Precio sin descuento"] = self.data[
                "Precio sin descuento"
            ].map(lambda value: "" if pd.isna(value) else "{:,.2f}".format(value))
            self.data[["Precio con descuento", "Precio sin descuento"]] = (
                self.data[["Precio con descuento", "Precio sin descuento"]]
                .replace({",": ";", "\\.": ","}, regex=True)
                .replace({";": "."}, regex=True)
            )
            self.data["Restaurante"].replace(" -.+", "", regex=True, inplace=True)
            self.data["Popular"].replace({"True": "popular", "False": ""}, inplace=True)
            self.data["Disponible"] = self.data["Disponible"].map(ENUM_RESTAURANT_STATUS)
            self.log.info("The Data has been cleaned successfully")
        except Exception as error:
            self.log.debug("Data wasn't cleaned successfully")
            print_error_detail(error)
            raise

    def transformar_data(self) -> None:
        if self.data.empty:
            self.log.info("No data available to transform")
            return

        text_columns = ["Producto", "Descripcion", "Restaurante", "Categoria"]
        for column in text_columns:
            if column in self.data.columns:
                self.data[column] = self.data[column].fillna("").astype(str).str.strip()

        self.log.info("Additional data transformation completed")

    def exportar_data(self) -> None:
        self.log.info("Saving CSV data")
        if not self.data.empty:
            nombre_archivo = f'Rappi_{self.hoy.strftime("%Y-%m-%d")}_{len(self.data.index)}.csv'
            ruta_archivo = join(self.ruta_data, nombre_archivo)
            self.data.to_csv(ruta_archivo, index=False, sep=";", encoding="utf-8-sig")
            self.log.info(f"Exported CSV file: {nombre_archivo}")
        else:
            self.log.info("The data wasn't saved because is empty")

    def get_discounts(self, item: list[dict] | None):
        return item[0]["price"] if item and len(item) > 0 else np.nan

    def consulta_restaurantes(self) -> None:
        try:
            self.log.info("Modifying the Initial Header for later use")
            if self.use_playwright_bootstrap:
                user_credentials = self._send_api_request_playwright(API_URL_GUEST)
            else:
                user_credentials = send_api_request(
                    API_URL_GUEST,
                    requests.post,
                    {},
                    self.rappi_header,
                )
            self.rappi_header["authorization"] = (
                user_credentials["token_type"] + " " + user_credentials["access_token"]
            )
            self.rappi_header["app-version"] = "1.120.4"
            self.rappi_header["app-version-name"] = "1.120.4"
            self.rappi_header["content-type"] = "application/json"
            self.rappi_header.pop("x-application-id")
            self.rappi_header.pop("x-guest-api-key", None)
            self.log.info("The Header was successfully modified")
        except KeyError as error:
            raise InvalidModificationRequestItemException("Header", error)

        try:
            if self.use_playwright_bootstrap:
                stores_catalog_response = self._send_api_request_playwright(
                    API_URL_STORES_CATALOG,
                    payload=self.rappi_variables,
                )
            else:
                stores_catalog_response = send_api_request(
                    API_URL_STORES_CATALOG,
                    requests.post,
                    {"json": self.rappi_variables},
                    self.rappi_header,
                )

            store_ids = set(stores_catalog_response["store_ids"])
        except KeyError as error:
            raise RestaurantsNotFoundException(
                f"The json response of the request {API_URL_STORES_CATALOG} doesn't have the {error} key"
            )

        progress = tqdm(total=len(store_ids), desc="Restaurant URL")
        self.restaurants = []
        for store_id in store_ids:
            self.restaurants.append(API_URL_STORES_INFO.format(store_id))
            progress.update(1)
        progress.close()

        if len(self.restaurants) > 0:
            self.log.info(f"N° Restaurants: {len(self.restaurants)}")
        else:
            raise RestaurantsNotFoundException(
                "The crawler couldn't extract any restaurants, so the process can't continue"
            )

        try:
            self.log.info("Modifying the Initial Payload for later use")
            self.rappi_variables.pop("states")
            self.log.info("The Payload was successfully modified")
        except KeyError as error:
            raise InvalidModificationRequestItemException("Payload", error)

    def consulta_restaurantes_productos(self, restaurant_data: dict) -> list[RappiProducto]:
        restaurant_name = restaurant_data.get("brand_name")
        restaurant_category = " · ".join(
            [tag.get("name") for tag in restaurant_data.get("tags", [])]
        )
        restaurant_status = restaurant_data.get("status", "OPEN")
        return [
            RappiProducto.model_validate(
                {
                    "popular": product.get("is_popular", False),
                    "producto": product.get("name"),
                    "descripcion": product.get("description"),
                    "precio_descuento": self.get_discounts(product.get("discounts")),
                    "precio": product.get("real_price"),
                    "restaurante": restaurant_name,
                    "estado": restaurant_status,
                    "categoria": restaurant_category,
                }
            )
            for product_category in restaurant_data.get("corridors", [])
            for product in product_category.get("products", [])
        ]


if __name__ == "__main__":
    rappi = Rappi()
    rappi.navegar()
