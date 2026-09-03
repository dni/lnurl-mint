"""A live mainnet smoke check for the spark funding-source backend.

Exercises the real code paths of lnurl_mint/spark.py against the real
Spark network: builds a wallet, then runs the backend's own functions
(create_invoice, the settlement/preimage lookups, sign_message,
fetch_node_info) end to end. Never moves funds by default - the melt
path is only probed up to its fee quote (prepare), and the
insufficient-funds sub-check additionally requires an empty wallet
balance before it attempts anything; paying for real is the explicit
--pay-invoice opt-in.

Requires the spark extra (`uv sync --extra spark`) and a Breez API key:

    uv run python scripts/spark_mainnet_check.py --api-key-file breez-api.key

By default the wallet is EPHEMERAL (fresh random seed, throwaway storage
dir) - fine for the checks here, since nothing is paid in or out. Pass
--mnemonic (or SPARK_MNEMONIC) and --storage to reuse a persistent
wallet instead. Either way the script prints the wallet's spark and
bitcoin addresses at the end, so it can be funded for a full
mint/melt round trip:

    # then, in a funded wallet, pay a real invoice end to end:
    uv run python scripts/spark_mainnet_check.py --api-key-file breez-api.key \
        --mnemonic "..." --storage ./spark-check \
        --pay-invoice lnbc...
"""

import argparse
import asyncio
import os
import sys
import tempfile
from os import urandom

import bolt11

try:
    from breez_sdk_spark import (  # type: ignore[import-not-found]
        GetInfoRequest,
        Network,
        PaymentRequest,
        PrepareSendPaymentRequest,
        ReceivePaymentMethod,
        ReceivePaymentRequest,
        SdkBuilder,
        Seed,
        SparkSigningOperator,
        SyncWalletRequest,
        default_config,
        init_logging,
    )
except ImportError as exc:  # pragma: no cover
    print(f"missing dependency: {exc} - install with `uv sync --extra spark`", file=sys.stderr)
    raise

from lnurl_mint import spark as spark_backend
from lnurl_mint.node import (
    LightningBackendConfig,
    create_invoice,
    fetch_node_info,
    invoice_preimage,
    is_invoice_settled,
    is_payment_complete,
    pay_invoice,
    payment_preimage,
    sign_message,
)
from lnurl_mint.signing import mint_pubkey, sign_note

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{f' - {detail}' if detail else ''}")


async def build_sdk(
    api_key: str,
    seed,
    storage: str,
    blackhole_endpoints: bool = False,
):
    config = default_config(network=Network.MAINNET)
    config.api_key = api_key
    config.real_time_sync_server_url = None
    if blackhole_endpoints:
        # every SSP/operator endpoint pointed at a discard address: an
        # instant connection-refused, indistinguishable from an outage at
        # the SDK's transport layer - used by the outage self-test below
        BLACKHOLE = "https://127.0.0.1:9"
        config.spark_config.ssp_config.base_url = BLACKHOLE
        config.spark_config.signing_operators = [
            SparkSigningOperator(
                id=op.id,
                identifier=op.identifier,
                address=BLACKHOLE,
                identity_public_key=op.identity_public_key,
                ca_cert_pem=op.ca_cert_pem,
            )
            for op in config.spark_config.signing_operators
        ]
    seed_used = seed
    try:  # sdk-internal logs land next to the storage dir when they can
        init_logging(storage, None, None)
    except Exception:
        pass
    builder = SdkBuilder(config=config, seed=seed_used)
    await builder.with_default_storage(storage_dir=storage)
    return await builder.build()


async def outage_self_test(api_key: str, seed, storage: str) -> None:
    """Proves the backend's remote-probe choices against the shipped
    wheel: with every endpoint unreachable, sync_wallet and get_info
    still succeed (breez-sdk-spark 0.23 swallows sub-sync failures and
    reads identity locally) while get_user_settings - what the backend
    brackets its reconciliation and health checks with - raises. If a
    future wheel changes any of this, this check flags it."""
    print("\noutage self-test (same wallet + storage, endpoints black-holed):")
    sdk = await build_sdk(api_key, seed, storage, blackhole_endpoints=True)
    try:
        try:
            await sdk.sync_wallet(request=SyncWalletRequest())
            check("sync_wallet resolves Ok during an outage (why it proves nothing)", True)
        except Exception as exc:
            check("sync_wallet resolves Ok during an outage (why it proves nothing)", False, str(exc)[:60])
        try:
            await sdk.get_info(request=GetInfoRequest(ensure_synced=None))
            check("get_info resolves Ok during an outage (local-only)", True)
        except Exception as exc:
            check("get_info resolves Ok during an outage (local-only)", False, str(exc)[:60])
        try:
            await sdk.get_user_settings()
            check("the probe's coordinator leg (get_user_settings) propagates the outage", False, "resolved Ok")
        except Exception as exc:
            check(
                "the probe's coordinator leg (get_user_settings) propagates the outage",
                True,
                f"{type(exc).__name__}",
            )
        try:
            await sdk.receive_payment(
                request=ReceivePaymentRequest(
                    payment_method=ReceivePaymentMethod.BOLT11_INVOICE(
                        description="lnurl-mint connectivity probe",
                        amount_sats=1,
                        expiry_secs=None,
                        payment_hash=None,
                        receiver_identity_public_key=None,
                    )
                )
            )
            check("the probe's SSP leg (invoice creation) propagates the outage", False, "resolved Ok")
        except Exception as exc:
            check(
                "the probe's SSP leg (invoice creation) propagates the outage",
                True,
                f"{type(exc).__name__}",
            )
    finally:
        try:
            await sdk.disconnect()
        except Exception:
            pass


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key-file", default="breez-api.key", help="file containing the Breez API key")
    parser.add_argument(
        "--mnemonic",
        default=os.environ.get("SPARK_MNEMONIC"),
        help="BIP39 mnemonic of a persistent wallet (default: $SPARK_MNEMONIC, or an ephemeral seed if unset)",
    )
    parser.add_argument("--storage", default=None, help="storage dir for a persistent wallet (default: tempdir)")
    parser.add_argument("--pay-invoice", default=None, help="melt-path e2e: actually pay this bolt11 invoice")
    args = parser.parse_args()

    api_key = os.environ.get("BREEZ_API_KEY") or open(args.api_key_file).read().strip()
    storage = args.storage or tempfile.mkdtemp(prefix="spark-mainnet-check-")
    ephemeral = args.mnemonic is None
    print(f"building spark wallet (mainnet, {'ephemeral seed' if ephemeral else 'persistent seed'}, {storage}) ...")
    # an unfunded throwaway mnemonic for ephemeral runs (raw entropy would
    # work for the wallet, but the LUD-25 signing key derives from the
    # mnemonic - BIP39 and raw entropy are NOT interchangeable seeds)
    mnemonic = (
        args.mnemonic or "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    )
    seed = Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
    sdk = await build_sdk(api_key, seed, storage)
    # the backend's own functions read the process-wide singleton - the
    # test hook swaps in this wallet, exactly what the unit suite's fakes do
    spark_backend._reset_sdk_for_testing(sdk)

    # a stand-in config: only backend/spark fields are read once the
    # singleton exists (the wallet was built explicitly above)
    backend_config = LightningBackendConfig(backend="spark", spark_mnemonic=mnemonic, spark_storage_dir=storage)
    try:
        print("\nnode info / identity:")
        info = await fetch_node_info(backend_config)
        check("fetch_node_info", bool(info.uri), f"identity {info.uri[:16]}...")

        print("\nmint path (invoice creation, no funds move):")
        amount_msat = 1_000_000  # 1000 sats
        pr, preimage = await create_invoice(amount_msat, backend_config)
        check("create_invoice returned an invoice", pr.startswith("lnbc"))
        check("create_invoice returns no preimage (the SSP holds it)", preimage is None)
        decoded = bolt11.decode(pr)
        check(
            "invoice decodes with amount and payment hash",
            decoded.amount_msat == amount_msat and decoded.has_payment_hash,
        )
        payment_hash = decoded.payment_hash
        check("unsettled invoice is not a note", (await is_invoice_settled(payment_hash, backend_config)) is False)
        check("unsettled invoice has no preimage", (await invoice_preimage(payment_hash, backend_config)) is None)

        print("\nmelt-path lookups on an unknown payment:")
        unknown = urandom(32).hex()
        # absence is indeterminate for this backend (never a bare False:
        # the crash-window + swallowed-sync reasoning, see
        # spark._is_payment_complete_spark) - after giving the row every
        # chance via the gated forced sync, which runs for real here
        try:
            await is_payment_complete(unknown, backend_config)
            check("unknown payment stays indeterminate (raises, never False)", False, "answered")
        except ValueError as exc:
            check("unknown payment stays indeterminate (raises, never False)", True, str(exc)[:50])
        check("unknown payment has no preimage", (await payment_preimage(unknown, backend_config)) is None)

        print("\noffline verification (LUD-25, seed-derived key):")
        # the SDK cannot produce the spec's Lightning-Signed-Message digest,
        # so the backend signs it locally with a dedicated key derived from
        # the wallet's seed (m/25'/0'/0') - spec-conformant: mintPubkey may
        # be any secp256k1 key, and wallets verify identically to lnd/cln
        h = urandom(32).hex()
        r_s, recovery_id = await sign_message(f"LNURLcash:{amount_msat}:{h}", backend_config)
        signature = (r_s + bytes([recovery_id])).hex()
        pubkey = await mint_pubkey(backend_config)
        from lnurl_mint.signing import verify_note

        check(
            "sign_message produces a spec-verifiable signature",
            pubkey is not None and verify_note(pubkey, h, amount_msat, signature),
        )
        check("sign_note signs with the same key", (await sign_note(h, amount_msat, backend_config)) == signature)
        check(
            "mint_pubkey is the dedicated derived key (not the wallet identity)",
            pubkey is not None and pubkey != info.uri,
        )

        print("\nfundable addresses (for a full round trip later):")
        spark_address = (
            await sdk.receive_payment(
                request=ReceivePaymentRequest(payment_method=ReceivePaymentMethod.SPARK_ADDRESS())
            )
        ).payment_request
        btc_address = (
            await sdk.receive_payment(
                request=ReceivePaymentRequest(payment_method=ReceivePaymentMethod.BITCOIN_ADDRESS(new_address=True))
            )
        ).payment_request
        print(f"  spark: {spark_address}")
        print(f"  btc:   {btc_address}")

        if args.pay_invoice is None:
            print("\nmelt path (quote only - nothing is paid):")
            # a second self-issued invoice stands in for the payee: the
            # prepare round trip is the same one a real melt makes, and an
            # unfunded wallet failing it with InsufficientFunds still
            # proves the plumbing end to end
            target = (
                await sdk.receive_payment(
                    request=ReceivePaymentRequest(
                        payment_method=ReceivePaymentMethod.BOLT11_INVOICE(
                            description="melt-path quote probe",
                            amount_sats=1000,
                            expiry_secs=None,
                            payment_hash=None,
                            receiver_identity_public_key=None,
                        )
                    )
                )
            ).payment_request
            try:
                quote = await sdk.prepare_send_payment(
                    request=PrepareSendPaymentRequest(payment_request=PaymentRequest.INPUT(input=target))
                )
                method = quote.payment_method
                check(
                    "prepare_send_payment quoted", method.is_BOLT11_INVOICE(), f"fee {method.lightning_fee_sats} sats"
                )
            except Exception as exc:
                check(
                    "prepare_send_payment answered cleanly", "insufficient" in str(exc).lower(), f"{type(exc).__name__}"
                )
            # the unfunded wallet is the exact scenario the
            # insufficient-funds restore exists for: prepare quotes fine,
            # send fails selecting leaves (provably pre-transfer), and the
            # confirmation must answer a clean False - not wedge the note.
            # Only attempted on a genuinely empty balance: on a funded
            # wallet this call would really pay, and the explicit opt-in
            # for that is --pay-invoice below
            from lnurl_mint.node import PaymentFailed

            balance_sats = (await sdk.get_info(request=GetInfoRequest(ensure_synced=None))).balance_sats
            if balance_sats > 0:
                print(f"  [--] underfunded-melt check skipped: wallet holds {balance_sats} sats")
            else:
                try:
                    await pay_invoice(target, backend_config, fee_limit_msat=10_000_000)
                    check("underfunded melt is a provable non-payment", False, "paid?!")
                except PaymentFailed as exc:
                    if "insufficient" not in str(exc).lower():
                        check("underfunded melt is a provable non-payment", False, str(exc)[:60])
                    else:
                        not_paid = await is_payment_complete(bolt11.decode(target).payment_hash, backend_config)
                        check("underfunded melt is a provable non-payment", not_paid is False)
        else:
            print("\nmelt path (paying for real, as requested):")
            result = await pay_invoice(args.pay_invoice, backend_config, fee_limit_msat=10_000_000)
            decoded_pay = bolt11.decode(args.pay_invoice)
            completed = await is_payment_complete(decoded_pay.payment_hash, backend_config)
            preimage_out = await payment_preimage(decoded_pay.payment_hash, backend_config)
            check(
                "pay_invoice completed with matching preimage and fee",
                completed and preimage_out == result.preimage and result.fee_msat is not None,
                f"fee {result.fee_msat} msat",
            )
    finally:
        await spark_backend.shutdown()

    # after the singleton's shutdown, so the outage instance has the
    # storage dir to itself - it reuses the SAME wallet and storage this
    # run built, i.e. the degraded-after-working state the probes exist for
    await outage_self_test(api_key, seed, storage)

    print()
    failed = [name for name, ok in CHECKS if not ok]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
