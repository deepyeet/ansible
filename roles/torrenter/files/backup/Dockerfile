FROM alpine/git

RUN apk add --no-cache bash rsync curl

WORKDIR /app

COPY ./daily-data-backup.sh /app

RUN chmod +x /app/daily-data-backup.sh

ENTRYPOINT ["/app/daily-data-backup.sh"]
