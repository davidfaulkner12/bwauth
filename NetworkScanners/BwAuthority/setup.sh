#!/bin/bash -e

SCANNER_DIR=$(dirname "$0")
# readlink -f does not work on Mac
if [ `uname` != "Darwin" ]
then
  SCANNER_DIR=$(readlink -f "$SCANNER_DIR")
fi

PYTHON=$(which python2.7 || which python2.6)

# 1. Install python if needed
if [ -z "$(which $PYTHON)" ]
then
  echo "We need python2.7 or 2.6 to be in the path."
  echo "If you are on a Debian or Ubuntu system, you can try: "
  echo " sudo apt-get install python2.7 python2.7-dev libpython2.7-dev libsqlite3-dev python-virtualenv autoconf2.13 automake make libevent-dev"
  exit 1
fi

if [ -z "$(which virtualenv)" ]
then
  echo "We need virtualenv to be in the path. If you are on a debian system, try:"
  echo " sudo apt-get install python-dev libsqlite3-dev python-virtualenv autoconf2.13 automake make libevent-dev"
  exit 1
fi

# 2. Ensure TorCtl submodule is added
pushd ../../
./add_torctl.sh
popd

# 3. Compile tor 0.2.8
if [ ! -x ../../../tor/src/or/tor ]
then
  pushd ../../../
  git clone https://git.torproject.org/tor.git tor
  cd tor
  git checkout release-0.2.8
  ./autogen.sh
  ./configure --disable-asciidoc
  make -j4
  popd
fi

# 4. Initialize virtualenv
if [ ! -f bwauthenv/bin/activate ]
then
  virtualenv -p $PYTHON bwauthenv
fi
source bwauthenv/bin/activate

# 5. Install new pip
pip install -r $SCANNER_DIR/requirements.txt

# 6.1 Prepare the cron script here, otherwise pausing the madness
cp cron.sh cron-mine.sh

# 7. Inform user what to do
echo
echo "If we got this far, everything should be ready!"
echo
echo "In order to setup the cron job, please run ./setup_cron.sh"
echo "Start the scan with ./run_scan.sh"
echo "You can manually run ./cron-mine.sh manually to check results"
echo "Detailed logs are in ./data/scanner.*/bw.log."
echo "Progress can also be inferred from files in ./data/scanner.*/scan-data"
