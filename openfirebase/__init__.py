"""openfirebase - an independent, open-source LOCAL reimplementation of the core
Firebase developer primitives, for local development, testing, and offline work.

This package is NOT affiliated with, endorsed by, or sponsored by the vendor whose
APIs it is compatible with. Vendor names are used only nominatively to describe API
compatibility. It implements a compatible SUBSET and is not intended for production.
"""

from .firestore import Firestore, Query
from .rtdb import RealtimeDatabase
from .auth import AuthService, AuthError
from .functions import FunctionRegistry, trigger
from .hosting import Hosting

__version__ = "0.1.0"

__all__ = [
    "Firestore",
    "Query",
    "RealtimeDatabase",
    "AuthService",
    "AuthError",
    "FunctionRegistry",
    "trigger",
    "Hosting",
    "__version__",
]
