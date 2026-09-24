"""Parser module — kurigram client + text preprocessing + photo download."""

__version__ = '5.0.0'
__author__ = 'Survival Map Team'

# DBAdapter общий для parser/nlp_processor (живёт в core/). Импорт тянет цепочку
# core.db.dbconnect → asyncpg — в контейнере parser asyncpg установлен
# (parser/requirements.txt), так что это безопасно.
from common.db_adapter import DBAdapter

__all__ = ['DBAdapter']
