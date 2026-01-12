FROM alpine:3.18
RUN apk add --no-cache bash curl jq

WORKDIR /app
COPY mam-updater.sh .
RUN chmod +x mam-updater.sh

CMD ["./mam-updater.sh"]