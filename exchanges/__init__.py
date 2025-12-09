"""
Exchange clients module for cross-exchange-arbitrage.
This module provides a unified interface for different exchange implementations.
"""

from .base import BaseExchangeClient, query_retry
try:
    from .edgex import EdgeXClient
except ImportError:
    EdgeXClient = None
try:
    from .backpack import BackpackClient
except ImportError:
    BackpackClient = None
try:
    from .extended import ExtendedClient
except ImportError:
    ExtendedClient = None

__all__ = [
    'BaseExchangeClient', 
    'EdgeXClient', 
    'BackpackClient',
    'ExtendedClient',
    'query_retry'
]

