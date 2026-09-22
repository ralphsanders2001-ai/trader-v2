#!/bin/bash
# Ship Robinhood trainer logs to OpenObserve after each run.
LOG_DIR="/home/ralph/robinhood-trainer/logs"
cd /home/ralph/robinhood-trainer
source venv/bin/activate

for f in scan.log monitor.log auto_trade.log eod.log weekly.log; do
    if [ -f "$LOG_DIR/$f" ]; then
        python -B oo_shipper.py --file "$LOG_DIR/$f" --source "${f%.log}"
    fi
done
