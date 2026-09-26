import json
import logging
import os
import signal
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

import oracledb
import pika
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("rabbitmq-oracle-consumer")


class PermanentMessageError(Exception):
    """The message is invalid and retrying will not fix it."""


def required_string(message: dict[str, Any], field: str) -> str:
    value = message.get(field)

    if not isinstance(value, str) or not value.strip():
        raise PermanentMessageError(
            f"Field {field!r} must contain a non-empty string"
        )

    return value.strip()


def validate_message(message: dict[str, Any]) -> dict[str, Any]:
    message_id = required_string(message, "message_id")
    customer_name = required_string(message, "customer_name")
    product_name = required_string(message, "product_name")
    order_status = required_string(message, "order_status")

    try:
        quantity = int(message["quantity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentMessageError(
            "Field 'quantity' must contain an integer"
        ) from exc

    if quantity <= 0:
        raise PermanentMessageError(
            "Field 'quantity' must be greater than zero"
        )

    try:
        unit_price = Decimal(str(message["unit_price"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise PermanentMessageError(
            "Field 'unit_price' must contain a valid number"
        ) from exc

    if unit_price < 0:
        raise PermanentMessageError(
            "Field 'unit_price' cannot be negative"
        )

    return {
        "message_id": message_id,
        "customer_name": customer_name,
        "product_name": product_name,
        "quantity": quantity,
        "unit_price": unit_price,
        "order_status": order_status,
    }


def create_oracle_connection() -> oracledb.Connection:
    dsn = oracledb.makedsn(
        host=os.environ["ORACLE_HOST"],
        port=int(os.environ["ORACLE_PORT"]),
        service_name=os.environ["ORACLE_SERVICE"],
    )

    return oracledb.connect(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=dsn,
    )


def insert_message(
    connection: oracledb.Connection,
    message: dict[str, Any],
    raw_payload: str,
) -> str:
    validated = validate_message(message)

    sql = """
        INSERT INTO rabbit_orders (
            message_id,
            customer_name,
            product_name,
            quantity,
            unit_price,
            order_status,
            message_payload
        )
        VALUES (
            :message_id,
            :customer_name,
            :product_name,
            :quantity,
            :unit_price,
            :order_status,
            :message_payload
        )
    """

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                {
                    **validated,
                    "message_payload": raw_payload,
                },
            )

        connection.commit()
        return "inserted"

    except oracledb.IntegrityError as exc:
        error_object = exc.args[0]

        # ORA-00001 means the primary key already exists.
        # This is treated as successful duplicate handling.
        if getattr(error_object, "code", None) == 1:
            connection.rollback()
            return "duplicate"

        connection.rollback()
        raise

    except oracledb.Error:
        connection.rollback()
        raise


def main() -> None:
    load_dotenv()

    oracle_connection = create_oracle_connection()

    rabbit_credentials = pika.PlainCredentials(
        os.environ["RABBITMQ_USER"],
        os.environ["RABBITMQ_PASSWORD"],
    )

    rabbit_parameters = pika.ConnectionParameters(
        host=os.environ["RABBITMQ_HOST"],
        port=int(os.environ["RABBITMQ_PORT"]),
        credentials=rabbit_credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )

    rabbit_connection = pika.BlockingConnection(rabbit_parameters)
    channel = rabbit_connection.channel()

    queue_name = os.environ["RABBITMQ_QUEUE"]

    channel.queue_declare(
        queue=queue_name,
        durable=True,
    )

    # Deliver only one unacknowledged message at a time.
    channel.basic_qos(prefetch_count=1)

    def shutdown_handler(signum: int, frame: object) -> None:
        del signum, frame

        logger.info("Shutdown requested")

        if rabbit_connection.is_open:
            rabbit_connection.add_callback_threadsafe(
                rabbit_connection.stop_ioloop
            )

    signal.signal(signal.SIGTERM, shutdown_handler)

    def process_message(
        callback_channel: pika.adapters.blocking_connection.BlockingChannel,
        method: pika.spec.Basic.Deliver,
        properties: pika.BasicProperties,
        body: bytes,
    ) -> None:
        del properties

        raw_payload = body.decode("utf-8")

        try:
            message = json.loads(raw_payload)

            if not isinstance(message, dict):
                raise PermanentMessageError(
                    "Message body must contain a JSON object"
                )

            result = insert_message(
                connection=oracle_connection,
                message=message,
                raw_payload=raw_payload,
            )

            callback_channel.basic_ack(
                delivery_tag=method.delivery_tag
            )

            logger.info(
                "Message processed: message_id=%s result=%s",
                message.get("message_id"),
                result,
            )

        except (json.JSONDecodeError, UnicodeDecodeError, PermanentMessageError) as exc:
            logger.error(
                "Invalid message rejected: error=%s payload=%r",
                exc,
                body,
            )

            # requeue=False prevents an invalid message from looping forever.
            callback_channel.basic_nack(
                delivery_tag=method.delivery_tag,
                requeue=False,
            )

        except oracledb.Error as exc:
            logger.exception(
                "Oracle operation failed; message will be requeued: %s",
                exc,
            )

            callback_channel.basic_nack(
                delivery_tag=method.delivery_tag,
                requeue=True,
            )

    channel.basic_consume(
        queue=queue_name,
        on_message_callback=process_message,
        auto_ack=False,
    )

    logger.info("Connected to Oracle")
    logger.info("Waiting for messages from queue %s", queue_name)

    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        logger.info("Consumer interrupted")
        channel.stop_consuming()
    finally:
        if rabbit_connection.is_open:
            rabbit_connection.close()

        oracle_connection.close()
        logger.info("Connections closed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Consumer stopped unexpectedly: %s", exc)
        sys.exit(1)
