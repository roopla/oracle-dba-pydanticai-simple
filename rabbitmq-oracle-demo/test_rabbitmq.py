import os
import sys

import pika
from dotenv import load_dotenv


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

    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        queue_name = os.environ["RABBITMQ_QUEUE"]

        channel.queue_declare(
            queue=queue_name,
            durable=True,
        )

        print("RabbitMQ connection succeeded")
        print(f"Queue ready: {queue_name}")

        connection.close()

    except pika.exceptions.AMQPError as exc:
        print(f"RabbitMQ connection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
