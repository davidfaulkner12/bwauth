#!/bin/sh

# Number of applications to run.
SCANNERS_PER_TOR_COUNT=4
TOR_COUNT=2
SCANNER_COUNT=$(($SCANNERS_PER_TOR_COUNT * $TOR_COUNT + 1))

# This tor must have the w status line fix as well as the stream bw fix
# Ie git master or 0.2.2.x
TOR_EXE=../../../tor/src/or/tor
PYTHONPATH=../../../SQLAlchemy-0.7.10/lib:../../../Elixir-0.7.1/

! [ -e "./local.cfg" ] || . "./local.cfg"

for n in `seq $SCANNER_COUNT`; do
  PIDFILE=./data/scanner.${n}/bwauthority.pid
  if [ -f $PIDFILE ]; then
    echo "Killing off scanner $n."
    kill -9 `head -1 $PIDFILE` && rm $PIDFILE
  fi
done

KILLED_TOR=false
for n in `seq $TOR_COUNT`; do
  PIDFILE=./data/tor.${n}/tor.pid
  if [ -f $PIDFILE ]; then
    if kill -0 `head -1 $PIDFILE` 2>/dev/null; then # it is a running process and we may send signals to it
  	  kill `head -1 $PIDFILE`
  	  if [ $? -eq 0 ]; then
  	    KILLED_TOR=true
  	  fi
    fi
  fi
done

sleep 5
