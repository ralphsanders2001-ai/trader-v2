FROM python:3.14-slim

# Trader V2 runtime deps (scikit-learn + xgboost + robin-stocks + flask)
RUN pip install --no-cache-dir \
    flask robin-stocks==3.4.0 numpy pandas scikit-learn xgboost yfinance requests

RUN pip install --no-cache-dir tzdata \
 && ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime && echo "America/New_York" > /etc/timezone

# v2 code lives at the hardcoded path /home/ralph/trader-v2
COPY app /home/ralph/trader-v2
WORKDIR /home/ralph/trader-v2

ENV HOME=/home/ralph
RUN mkdir -p /home/ralph/.tokens /home/ralph/trader-v2/logs /mnt/backup-mount

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8091
ENTRYPOINT ["/entrypoint.sh"]
