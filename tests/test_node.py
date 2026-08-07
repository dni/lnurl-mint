import httpx

from lnurl_mint.node import _cln_pay_failure_reason, _lnd_failure_reason


def test_cln_pay_failure_reason_maps_known_codes():
    res = httpx.Response(500, json={"code": 205, "message": "Destination unreachable"})
    assert _cln_pay_failure_reason(res) == "Could not find a route to pay this invoice."


def test_cln_pay_failure_reason_falls_back_to_clns_own_message_for_unmapped_codes():
    res = httpx.Response(500, json={"code": 999, "message": "Some other cln error"})
    assert _cln_pay_failure_reason(res) == "Some other cln error"


def test_cln_pay_failure_reason_falls_back_to_status_code_for_an_unparseable_body():
    res = httpx.Response(500, content=b"not json")
    assert _cln_pay_failure_reason(res) == "Payment failed (500)."


def test_lnd_failure_reason_maps_known_reasons():
    assert _lnd_failure_reason("FAILURE_REASON_NO_ROUTE") == "Could not find a route to pay this invoice."


def test_lnd_failure_reason_falls_back_to_the_raw_reason_for_unmapped_ones():
    assert _lnd_failure_reason("FAILURE_REASON_ERROR") == "Payment failed: FAILURE_REASON_ERROR."
