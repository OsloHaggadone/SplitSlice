"""Runs slow work (network calls) on a background thread until its result is needed."""

import threading


class Background:
    """fn(*args) on a daemon thread (Ctrl+C never waits for it). result()
    waits, then returns its value or raises its error."""

    def __init__(self, fn, *args):
        self._value = self._error = None
        self._thread = threading.Thread(target=self._run, args=(fn, args), daemon=True)
        self._thread.start()

    def _run(self, fn, args):
        try:
            self._value = fn(*args)
        except BaseException as e:
            self._error = e

    def result(self):
        while self._thread.is_alive():
            self._thread.join(0.1)  # short steps, so Ctrl+C gets through on Windows
        if self._error is not None:
            raise self._error
        return self._value
