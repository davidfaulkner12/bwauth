#!/bin/bash -e

SCANNER_DIR=$(dirname "$0")
if [ `uname` != "Darwin" ]
then
  SCANNER_DIR=$(readlink -f "$SCANNER_DIR")
fi

# 6.2 Prepare cron script
echo -e "45 0-23 * * * $SCANNER_DIR/cron-mine.sh" | crontab
echo -e "@reboot $SCANNER_DIR/run_scan.sh\n`crontab -l`" | crontab
echo "Prepared crontab. Current crontab: "
crontab -l
