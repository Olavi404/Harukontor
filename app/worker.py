import asyncio
from datetime import timedelta
import logging

from app.central_bank import (
    build_interbank_jwt,
    get_local_bank_id,
    post_interbank_transfer,
    refresh_banks_cache,
    resolve_destination_bank_from_cache,
    send_heartbeat,
)
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import Transfer
from app import services


settings = get_settings()
logger = logging.getLogger("branch_bank.worker")
MAX_HEARTBEAT_INTERVAL_SECONDS = 30 * 60


def heartbeat_interval_seconds() -> int:
    configured = max(1, int(settings.heartbeat_interval_seconds))
    if configured > MAX_HEARTBEAT_INTERVAL_SECONDS:
        logger.warning(
            "worker.heartbeat.interval_clamped configured=%s clamped=%s",
            configured,
            MAX_HEARTBEAT_INTERVAL_SECONDS,
        )
        return MAX_HEARTBEAT_INTERVAL_SECONDS
    return configured


async def heartbeat_loop() -> None:
    sleep_seconds = heartbeat_interval_seconds()
    while True:
        db = SessionLocal()
        try:
            await send_heartbeat(db)
        except Exception as exc:
            logger.warning("worker.heartbeat.failed error=%s", exc)
        finally:
            db.close()
        await asyncio.sleep(sleep_seconds)


async def sync_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            await refresh_banks_cache(db)
        except Exception as exc:
            logger.warning("worker.sync.failed error=%s", exc)
        finally:
            db.close()
        await asyncio.sleep(settings.bank_sync_interval_seconds)


async def pending_retry_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            due = services.list_due_pending(db)
            for transfer in due:
                if transfer.pending_since and services.now_utc() - transfer.pending_since > timedelta(hours=settings.pending_timeout_hours):
                    services.mark_timeout_and_refund(db, transfer)
                    db.commit()
                    logger.warning("worker.transfer.timeout transfer_id=%s", transfer.transfer_id)
                    continue

                destination_prefix = transfer.destination_account[:3]
                destination_bank = resolve_destination_bank_from_cache(db, destination_prefix)
                if destination_bank is None:
                    services.set_next_retry(transfer)
                    db.commit()
                    logger.info("worker.transfer.retry_scheduled transfer_id=%s reason=missing_destination_bank", transfer.transfer_id)
                    continue

                source_bank_id = get_local_bank_id(db)
                token = build_interbank_jwt(
                    transfer.transfer_id,
                    transfer.source_account,
                    transfer.destination_account,
                    transfer.converted_amount or transfer.amount,
                    source_bank_id,
                    destination_bank.bank_id,
                )

                try:
                    await post_interbank_transfer(destination_bank.address, token)
                    services.finalize_completed_transfer(
                        transfer,
                        converted_amount=transfer.converted_amount,
                        exchange_rate=transfer.exchange_rate,
                        rate_ts=transfer.rate_captured_at,
                    )
                    db.commit()
                    logger.info("worker.transfer.completed transfer_id=%s", transfer.transfer_id)
                except Exception:
                    services.set_next_retry(transfer)
                    db.commit()
                    logger.info("worker.transfer.retry_scheduled transfer_id=%s reason=destination_unavailable", transfer.transfer_id)
        finally:
            db.close()

        await asyncio.sleep(settings.pending_retry_poll_seconds)


async def main() -> None:
    Base.metadata.create_all(bind=engine)
    await asyncio.gather(heartbeat_loop(), sync_loop(), pending_retry_loop())


if __name__ == "__main__":
    asyncio.run(main())
