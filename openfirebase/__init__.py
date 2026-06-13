"""openfirebase - an independent, open-source LOCAL reimplementation of the core
Firebase developer primitives, for local development, testing, and offline work.

This package is NOT affiliated with, endorsed by, or sponsored by the vendor whose
APIs it is compatible with. Vendor names are used only nominatively to describe API
compatibility. It implements a compatible SUBSET and is not intended for production.
"""

from .firestore import Firestore, Query, FieldValue, WriteBatch, Transaction, TransactionError
from .rtdb import RealtimeDatabase, RTDBQuery, OnDisconnect
from .auth import AuthService, AuthError
from .functions import (
    FunctionRegistry, FunctionError, trigger,
    ON_CREATE, ON_WRITE, ON_UPDATE, ON_DELETE,
    ON_AUTH_USER_CREATE, ON_AUTH_USER_DELETE,
    ON_STORAGE_FINALIZE, ON_STORAGE_DELETE,
    ON_PUBSUB_MESSAGE, ON_SCHEDULE,
)
from .hosting import Hosting
from .cloudstorage import CloudStorage, StorageBucket, ObjectNotFoundError
from .rules import RulesEngine, RulesError, PermissionDenied
from .remoteconfig import RemoteConfig, RemoteConfigError
from .messaging import CloudMessaging, MessagingError
from .appcheck import AppCheck, AppCheckError

__version__ = "0.4.0"

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
    "FunctionError",
    "trigger",
    "ON_CREATE",
    "ON_WRITE",
    "ON_UPDATE",
    "ON_DELETE",
    "ON_AUTH_USER_CREATE",
    "ON_AUTH_USER_DELETE",
    "ON_STORAGE_FINALIZE",
    "ON_STORAGE_DELETE",
    "ON_PUBSUB_MESSAGE",
    "ON_SCHEDULE",
    "Hosting",
    "CloudStorage",
    "StorageBucket",
    "ObjectNotFoundError",
    "RulesEngine",
    "RulesError",
    "PermissionDenied",
    "RemoteConfig",
    "RemoteConfigError",
    "CloudMessaging",
    "MessagingError",
    "AppCheck",
    "AppCheckError",
    "__version__",
]
