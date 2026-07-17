# MANAGED BY deploy/install_customs_export_cron.sh - DO NOT EDIT IN PLACE
# INSTALL TARGET: /etc/cron.d/sanjuk-customs-export
SHELL=/bin/sh
PATH=/usr/bin:/bin
HOME=/home/kanzaka110
MAILTO=""

# GCP server timezone is UTC. Daily safety net: 03:20 UTC = KST 12:20.
# Supervisor hard deadline: 350s + 10s kill grace = 360s.
20 3 * * * kanzaka110 /usr/bin/env -i HOME=/home/kanzaka110 PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC /usr/bin/timeout --signal=TERM --kill-after=10s 350s /bin/bash --noprofile --norc /home/kanzaka110/Sanjuk-Stock-Simulator/deploy/run_customs_export_logged.sh

# Release windows for 1st/11th/21st publication + lag: 09:20 UTC = KST 18:20.
20 9 1-3,11-13,21-23 * * kanzaka110 /usr/bin/env -i HOME=/home/kanzaka110 PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC /usr/bin/timeout --signal=TERM --kill-after=10s 350s /bin/bash --noprofile --norc /home/kanzaka110/Sanjuk-Stock-Simulator/deploy/run_customs_export_logged.sh
