import asyncio
from datetime import timedelta

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


async def heartbeat_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            await send_heartbeat(db)
        except Exception:
            pass
        finally:
            db.close()
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def sync_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            await refresh_banks_cache(db)
        except Exception:
            pass
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
                    continue

                destination_prefix = transfer.destination_account[:3]
                destination_bank = resolve_destination_bank_from_cache(db, destination_prefix)
                if destination_bank is None:
                    services.set_next_retry(transfer)
                    db.commit()
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
                except Exception:
                    services.set_next_retry(transfer)
                    db.commit()
        finally:
            db.close()

        await asyncio.sleep(settings.pending_retry_poll_seconds)


async def main() -> None:
    Base.metadata.create_all(bind=engine)
    await asyncio.gather(heartbeat_loop(), sync_loop(), pending_retry_loop())


if __name__ == "__main__":
    asyncio.run(main())
