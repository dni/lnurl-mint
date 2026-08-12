import logging

import lnurl_mint.errors as errors_module


def test_log_internal_error_does_not_crash_when_error_log_is_unwritable(monkeypatch, tmp_path):
    # regression: an unwritable data directory (see errors.py's delay=True)
    # must not crash a request that hits log_internal_error - error.log is
    # a diagnostic side channel, not something a caller's own request
    # should ever fail over
    unwritable_dir = tmp_path / "unwritable"
    unwritable_dir.mkdir(mode=0o500)
    broken_logger = logging.getLogger("lnurl_mint.errors.test_unwritable")
    broken_logger.setLevel(logging.ERROR)
    broken_logger.propagate = False
    broken_logger.addHandler(logging.FileHandler(str(unwritable_dir / "error.log"), delay=True))
    monkeypatch.setattr(errors_module, "_logger", broken_logger)

    reference = errors_module.log_internal_error("boom", ValueError("something broke"))

    assert "reference:" in reference


def test_log_internal_error_still_logs_normally_when_writable(tmp_path, monkeypatch):
    logger = logging.getLogger("lnurl_mint.errors.test_writable")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    log_path = tmp_path / "error.log"
    logger.addHandler(logging.FileHandler(str(log_path), delay=True))
    monkeypatch.setattr(errors_module, "_logger", logger)

    reference = errors_module.log_internal_error("boom", ValueError("something broke"))

    logged = log_path.read_text()
    assert logged.count("boom") == 1
    assert reference.split("reference: ")[1].rstrip(").") in logged
