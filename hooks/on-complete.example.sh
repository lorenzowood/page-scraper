#!/bin/sh
# Example completion hook, in the spirit of qBittorrent's "run on torrent completion".
# Run this on the NAS / a trusted host that can see both the inbox share and the
# destination share. Page Scraper itself should only write to the locked-down inbox.
set -eu

echo "job ${JOB_ID} ${JOB_STATUS} failed=${JOB_FAILED}/${JOB_TOTAL} dir=${JOB_OUTPUT_DIR}"
# rsync -a --remove-source-files "${JOB_OUTPUT_DIR}/" "/mnt/nas/page-scraper/library/"
