"""openfirebase - an independent, open-source LOCAL reimplementation of the core
Firebase developer primitives, for local development, testing, and offline work.

This package is NOT affiliated with, endorsed by, or sponsored by the vendor whose
APIs it is compatible with. Vendor names are used only nominatively to describe API
compatibility. It implements a compatible SUBSET and is not intended for production.
"""

from .firestore import Firestore, Query, FieldValue, WriteBatch, Transaction, TransactionError
from .rtdb import RealtimeDatabase, RTDBQuery, OnDisconnect
from .auth import AuthService, AuthError
from .functions import FunctionRegistry, trigger
from .hosting import Hosting
from .cloudstorage import CloudStorage, StorageBucket, ObjectNotFoundError

__version__ = "0.2.0"

__all__ = [
    "Firestore",
    "Query",
    "FieldValue",
    "WriteBatch",
    "Transaction",
    "TransactionError",
    "RealtimeDatabase",
    "RTDBQuery",
    "OnDisconnect",
    "AuthService",
    "AuthError",
    "FunctionRegistry",
    "trigger",
    "Hosting",
    "CloudStorage",
    "StorageBucket",
    "ObjectNotFoundError",
    "__version__",
]
