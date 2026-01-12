FROM python:3.11-slim

# 1. Install dependencies: curl for healthchecks, gawk for parsing, jq for JSON
RUN apt-get update && \
    apt-get install -y curl gawk jq && \
    rm -rf /var/lib/apt/lists/*

# 2. Install python dependencies
RUN pip install requests

# 3. Create user 'ptp' (UID 1000)
RUN groupadd -g 1000 ptp && \
    useradd -u 1000 -g ptp -s /bin/bash ptp

# 4. Setup directories
WORKDIR /app
RUN chown 1000:1000 /app

# 5. Copy script
COPY start.sh .
RUN chmod +x start.sh

# 6. CRITICAL: Force python logs to stdout immediately
ENV PYTHONUNBUFFERED=1

# 7. Switch user
USER 1000:1000

CMD ["./start.sh"]
