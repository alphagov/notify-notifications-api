#!/bin/bash

CONCURRENCY=${CONCURRENCY:-4}

# Define a common command prefix
WORKER_CMD="celery --quiet -A run_celery.notify_celery worker --logfile=/dev/null --concurrency=$CONCURRENCY"
COMMON_CMD="$WORKER_CMD -Q"

case "$1" in
  worker)
    exec $WORKER_CMD
    ;;
  api)
    exec gunicorn -c /home/vcap/app/gunicorn_config.py application
    ;;
  api-local)
    exec flask run --host 0.0.0.0 --port $PORT
    ;;
  migration)
    exec flask db upgrade
    ;;
  functional-test-fixtures)
    exec flask command functional-test-fixtures
    ;;
  api-worker-retry-tasks)
    exec $COMMON_CMD retry-tasks
    ;;
  api-worker-letters)
    exec $COMMON_CMD create-letters-pdf-tasks,letter-tasks
    ;;
  api-worker-jobs)
    exec $COMMON_CMD database-tasks,job-tasks
    ;;
  api-worker-research)
    exec $COMMON_CMD research-mode-tasks
    ;;
  api-worker-sender)
    exec $COMMON_CMD send-sms-tasks,send-email-tasks
    ;;
  api-worker-sender-letters)
    exec $COMMON_CMD send-letter-tasks
    ;;
  api-worker-periodic)
    exec $COMMON_CMD periodic-tasks
    ;;
  api-worker-reporting)
    exec $COMMON_CMD reporting-tasks
    ;;
  api-worker-internal)
    # Only consume the notify-internal-tasks queue on this app so that Notify messages are processed as a priority
    exec $COMMON_CMD notify-internal-tasks
    ;;
  api-worker-receipts)
    exec $COMMON_CMD ses-callbacks,sms-callbacks
    ;;
  api-worker-service-callbacks)
    exec $COMMON_CMD service-callbacks,service-callbacks-retry
    ;;
  celery-beat)
    exec celery -A run_celery.notify_celery beat --loglevel=INFO
    ;;
  *)
    echo -e "'\033[31m'FATAL: missing argument'\033[0m'" && exit 1
    exit 1
    ;;
esac
