import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pika
from dotenv import load_dotenv


def create_message() -> dict[str, object]:
    return {
        "message_id": str(uuid.uuid4()),
        "customer_name": "Pradeep Test Customer",
        "product_name": "Oracle Test Product",
        "quantity": 2,
        "unit_price": str(Decimal("49.95")),
        "order_status": "NEW",
        "created_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    load_dotenv()

    credentials = pika.PlainCredentials(
        os.environ["RABBITMQ_USER"],
        os.environ["RABBITMQ_PASSWORD"],
    )

    parameters = pika.ConnectionParameters(
        host=os.environ["RABBITMQ_HOST"],
        port=int(os.environ["RABBITMQ_PORT"]),
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=30,
    )

    message = create_message()
    body = json.dumps(message).encode("utf-8")

    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        queue_name = os.environ["RABBITMQ_QUEUE"]

        channel.queue_declare(
            queue=queue_name,
            durable=True,
        )

        # Ask RabbitMQ to confirm that it accepted the publication.
        channel.confirm_delivery()

        channel.basic_publish(
            exchange="",
            routing_key=queue_name,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=pika.DeliveryMode.Persistent,
                message_id=str(message["message_id"]),
                timestamp=int(datetime.now(UTC).timestamp()),
                app_id="oracle-demo-producer",
            ),
            mandatory=True,
        )

        print("Message published successfully")
        print(json.dumps(message, indent=2))

        connection.close()

    except (pika.exceptions.AMQPError, ValueError) as exc:
        print(f"Publishing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
