#!/bin/sh

set -x

# Grab our libs
. "`dirname $0`/setup-lib.sh"

if [ -f $OURDIR/mpi-perf-done ]; then
    exit 0
fi

logtstart "mpi-perf"

maybe_install_packages libopenmpi-dev linux-tools-5.15.0-72
$SUDO sysctl kernel.perf_event_paranoid=-1
$SUDO sh -c " echo 0 > /proc/sys/kernel/kptr_restrict"
$SUDO cp /lib/linux-tools-5.15.0-72/perf /usr/bin/perf
$SUDO cp /lib/linux-tools-5.15.0-72/perf /bin/perf
logtend "mpi-perf"
touch $OURDIR/mpi-perf-done

