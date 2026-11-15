import logging
import time


class SafeLogger:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger('reviewtrail')

    def request(self, method, route, status, started):
        self.logger.info('%s %s %s %.1fms', method, route, status, (time.monotonic() - started) * 1000)

    def exception(self, error):
        self.logger.error('request failed: %s', type(error).__name__)
