from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Type, Union

from fastapi.datastructures import Default
from fastapi.params import Depends
from werkzeug.wrappers import Request as WerkzeugRequest, Response as WerkzeugResponse

# Import register_app from fast_routes separately
from frappeapi.fast_routes import (
	DELETE as _fast_delete,
	GET as _fast_get,
	HEAD as _fast_head,
	OPTIONS as _fast_options,
	PATCH as _fast_patch,
	POST as _fast_post,
	PUT as _fast_put,
	register_app,
)
from frappeapi.responses import JSONResponse
from frappeapi.routing import APIRouter


class FrappeAPI:
	def __init__(
		self,
		title: Optional[str] = "Frappe API",
		summary: Optional[str] = None,
		description: Optional[str] = None,
		version: Optional[str] = "0.1.0",
		servers: Optional[List[Dict[str, Union[str, Any]]]] = None,
		openapi_tags: Optional[List[Dict[str, Any]]] = None,
		terms_of_service: Optional[str] = None,
		contact: Optional[Dict[str, Union[str, Any]]] = None,
		license_info: Optional[Dict[str, Union[str, Any]]] = None,
		separate_input_output_schemas: bool = True,
		dependencies: Optional[Sequence[Depends]] = None,
		default_response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		middleware: Optional[Sequence] = None,
		exception_handlers: Optional[
			Dict[
				Union[int, Type[Exception]],
				Callable[[WerkzeugRequest, Exception], WerkzeugResponse],
			]
		] = None,
		# Feature flag for OpenAPI path style
		fastapi_path_format: bool = False,
		contract_app: Optional[str] = None,
	):
		self.title = title
		self.summary = summary
		self.description = description
		self.version = version
		self.servers = servers
		self.openapi_version: str = "3.1.0"
		self.openapi_tags = openapi_tags
		self.terms_of_service = terms_of_service
		self.contact = contact
		self.license_info = license_info
		self.separate_input_output_schemas = separate_input_output_schemas
		self.fastapi_path_format = fastapi_path_format
		assert self.title, "A title must be provided for OpenAPI, e.g.: 'My API'"
		assert self.version, "A version must be provided for OpenAPI, e.g.: '1.0.0'"

		self.exception_handlers: Dict[Type[Exception], Callable[[WerkzeugRequest, Exception], WerkzeugResponse]] = (
			{} if exception_handlers is None else dict(exception_handlers)  # type: ignore
		)
		self.router = APIRouter(
			title=self.title,
			version=self.version,
			openapi_version=self.openapi_version,
			summary=self.summary,
			description=self.description,
			webhooks=None,
			openapi_tags=self.openapi_tags,
			servers=self.servers,
			terms_of_service=self.terms_of_service,
			contact=self.contact,
			license_info=self.license_info,
			separate_input_output_schemas=self.separate_input_output_schemas,
			exception_handlers=self.exception_handlers,
			default_response_class=default_response_class,
			fastapi_path_format=self.fastapi_path_format,
		)
		self.router.contract_app = contract_app
		from frappeapi import errors

		# Every app: frappeapi's helpers raise ApiError subclasses (resolved by MRO in the route).
		# No existing app raises ApiError, so other responses are unchanged.
		self.exception_handlers.setdefault(errors.ApiError, errors.handle)
		self.openapi_schema: Optional[Dict[str, Any]] = None

		register_app(self)

	def openapi(self) -> Dict[str, Any]:
		if self.openapi_schema is None:
			self.openapi_schema = self.router.openapi()
		return self.openapi_schema

	# ------------------------------------------------------------------ #
	# Hybrid decorator helpers
	# ------------------------------------------------------------------ #

	def _dual(
		self,
		starlette_reg: Callable[[str], Callable[[Callable], Callable]],
		router_reg: Callable[..., Callable[[Callable], Callable]],
		*,
		path: str,
		response_model: Any,
		status_code: Optional[int],
		description: Optional[str],
		tags: Optional[List[Union[str, Enum]]],
		summary: Optional[str],
		include_in_schema: bool,
		response_class: Type[WerkzeugResponse],
		allow_guest: bool,
		xss_safe: bool,
	):
		# router_reg (e.g. self.router.get) creates and registers an APIRoute instance.
		# This APIRoute instance holds all metadata and the handle_request logic.
		# It needs to be created regardless of the fastapi_path_format mode so that
		# the route is available in self.router.routes for OpenAPI and potentially for
		# the patched handler if in FastAPI mode.
		api_route_decorator = router_reg(
			path=path,  # The FastAPI-style path template, e.g., "/items/{item_id}"
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,  # Frappe specific for @whitelist
			xss_safe=xss_safe,  # Frappe specific for @whitelist
		)

		# starlette_reg (e.g., _fast_get from fast_routes.py) returns a factory
		# for a pass-through decorator.
		pass_through_decorator_factory = starlette_reg(path)

		if self.fastapi_path_format:
			# FastAPI-style paths are primary.
			# The user's decorated function, when called by Frappe's standard dotted path mechanism,
			# should be the original, unprocessed function.
			# FastAPI-style paths will be intercepted and handled by the patched `frappe.api.handle`
			# using the APIRoute instance created by `api_route_decorator`.
			def wrapper(fn):
				# Ensure the APIRoute is created and registered by calling the decorator.
				# The result (a whitelisted, processing-wrapped function) is not what we
				# return from *this* wrapper in FastAPI mode for dotted path calls.
				_ = api_route_decorator(fn)

				# pass_through_decorator_factory(fn) returns the original `fn`.
				return pass_through_decorator_factory(fn)

			return wrapper
		else:
			# Dotted paths are primary and should be processed by APIRoute's logic.
			# Warn if the FastAPI-style path template (passed as `path`) contains placeholders,
			# as these won't be used for dotted path parameter extraction in this mode (yet).
			if "{" in path and "}" in path:
				import warnings

				warnings.warn(
					f"Endpoint defined with FastAPI-style path template ('{path}') while "
					f"'fastapi_path_format' is False. Path parameters from this template "
					f"will not be available for dotted path calls. "
					f"Support for path parameters in dotted paths is planned for a future update.",
					UserWarning,
					stacklevel=3,
				)

			def wrapper(fn):
				# api_route_decorator(fn) returns a whitelisted function that, when called
				# (e.g., by Frappe for a dotted path), executes APIRoute.handle_request.
				return api_route_decorator(fn)

			return wrapper

	# ------------------------------------------------------------------ #
	# Public HTTP verb decorators
	# ------------------------------------------------------------------ #

	def get(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_get,
			self.router.get,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def post(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_post,
			self.router.post,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def put(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_put,
			self.router.put,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def delete(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_delete,
			self.router.delete,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def patch(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_patch,
			self.router.patch,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def options(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_options,
			self.router.options,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def head(
		self,
		path: str,
		*,
		response_model: Any = Default(None),
		status_code: Optional[int] = None,
		description: Optional[str] = None,
		tags: Optional[List[Union[str, Enum]]] = None,
		summary: Optional[str] = None,
		include_in_schema: bool = True,
		response_class: Type[WerkzeugResponse] = Default(JSONResponse),
		# Frappe parameters
		allow_guest: bool = False,
		xss_safe: bool = False,
	):
		return self._dual(
			_fast_head,
			self.router.head,
			path=path,
			response_model=response_model,
			status_code=status_code,
			description=description,
			tags=tags,
			summary=summary,
			include_in_schema=include_in_schema,
			response_class=response_class,
			allow_guest=allow_guest,
			xss_safe=xss_safe,
		)

	def exception_handler(self, exc_class: Type[Exception]) -> Callable:
		"""
		Add an exception handler to the application.

		Exception handlers are used to handle exceptions that are raised during the processing of a request.
		"""

		def decorator(func: Callable[[WerkzeugRequest, Exception], WerkzeugResponse]):
			self.exception_handlers[exc_class] = func
			return func

		return decorator
