"""Parser configuration: constants, settings bootstrap."""

import logging
from zoneinfo import ZoneInfo

from common.settings import settings

logger = logging.getLogger(__name__)

KIEV_TZ = ZoneInfo("Europe/Kiev")

_MIN_WORKERS = 2
_MAX_WORKERS = 8
_SCALE_UP_QSIZE = 20
_IDLE_TIMEOUT = 15

# Healthcheck heartbeat interval (seconds)
_HEARTBEAT_INTERVAL = 5

# Batch insert flush interval (seconds)
_BATCH_FLUSH_INTERVAL = 0.1

# Stale photo cleanup interval (seconds)
_STALE_PHOTO_INTERVAL = 300

# Stale photo cutoff (seconds)
_STALE_PHOTO_CUTOFF_SECONDS = 70 * 60

# Queue maxsize
_QUEUE_MAXSIZE = 65

# Backpressure thresholds
_BACKPRESSURE_HIGH = 60
_BACKPRESSURE_LOW = 40

# Photo download concurrency limit
_PHOTO_DOWNLOAD_CONCURRENCY = 3
